"""Meta backend - Instagram Reels, Threads and Facebook Reels, posted directly.

This is the free route: your own Meta app, your own accounts, no reseller cut.
In Development mode it posts to accounts where you hold an app role, which is
all a single-operator setup needs; App Review is only for posting to accounts
you do not control.

Three platforms, two shapes of API:

* Instagram and Threads use a *container* flow - hand Meta a public URL, poll
  until it has downloaded and transcoded the file, then publish the container.
* Facebook Reels uses a three-phase upload - start, transfer, finish - on a
  separate upload host.

All three fetch the video themselves, so the clip needs a public URL. R2
presigned links are what this uses; a local-storage setup cannot publish here
because Meta cannot reach a file:// path.

Tokens expire after 60 days. `refresh_tokens()` extends them; run
`python scripts/meta_token.py --refresh` on a reminder, or the queue starts
failing two months after it last worked.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

from core import credentials
from core.config import settings
from core.publishers import PublishRequest, PublishResult
from core.storage import get_storage

log = logging.getLogger(__name__)

FACEBOOK_HOST = "https://graph.facebook.com"
THREADS_HOST = "https://graph.threads.net"
INSTAGRAM_HOST = "https://graph.instagram.com"

PLATFORMS = ("instagram", "threads", "facebook")

# Per-platform caption ceilings. Cutting to length beats a 400 from Meta.
CAPTION_LIMITS = {"instagram": 2200, "threads": 500, "facebook": 2200}

# How long a presigned clip URL stays valid. Meta downloads it once, early on,
# but a busy transcode queue can leave it sitting a while.
CLIP_URL_TTL_S = 6 * 3600

POLL_INTERVAL_S = 5.0


#: Meta's way of saying "not now". Code 4 is the application request limit;
#: subcode 1349210 is Instagram's content-publishing quota specifically, which
#: is 25 published posts per account per rolling 24 hours.
#:
#: Worth telling apart from every other failure, because the response is
#: completely different: a broken render fails the same way forever and should
#: be given up on, while this one succeeds by itself if you simply stop asking
#: for a while. Counting a rate limit as a failed attempt abandons a post over
#: something temporary.
RATE_LIMIT_MARKERS = (
    "application request limit reached",
    "1349210",
    "(code 4/",
    "(code 4)",
    "(code 17",
    "rate limit",
    "please retry",
)


def is_rate_limited(error: str | None) -> bool:
    """Whether this failure is Meta asking for less traffic, not a fault."""
    if not error:
        return False
    said = error.lower()
    return any(marker in said for marker in RATE_LIMIT_MARKERS)


#: Instagram's way of saying "that container is not ready yet". Code 9007 with
#: subcode 2207027 is worded "Media ID is not available", which reads like a
#: wrong id and sends you off checking the one thing that is fine.
#:
#: It is what a publish gets when Meta has not finished assembling the
#: container - and container readiness is eventually consistent, so a carousel
#: can report FINISHED and still be refused for a few seconds after. Waiting
#: and asking again is the documented answer, not a workaround.
NOT_READY_MARKERS = ("media id is not available", "2207027", "(code 9007")

#: How many times to re-ask, and how long to leave between. Three tries over
#: sixteen seconds; beyond that it is not a race, it is broken.
PUBLISH_ATTEMPTS = 3
PUBLISH_BACKOFF_S = 8.0


def is_not_ready(error: str | None) -> bool:
    """Whether Meta is refusing a container it has not finished building."""
    if not error:
        return False
    said = error.lower()
    return any(marker in said for marker in NOT_READY_MARKERS)


class MetaError(RuntimeError):
    """A Graph API call came back with an error we cannot retry past."""


@dataclass
class _Target:
    """One platform's account id, token and API host."""

    platform: str
    account_id: str
    token: str
    host: str


class MetaPublisher:
    name = "meta"

    def __init__(
        self,
        client: httpx.Client | None = None,
        sleep=time.sleep,
    ) -> None:
        self._client = client
        self._sleep = sleep
        self.version = settings.meta_graph_version
        self.timeout_s = settings.meta_publish_timeout_s

        if not (settings.has_instagram or settings.has_threads or settings.has_facebook):
            raise RuntimeError(
                "no Meta account is configured - set INSTAGRAM_USER_ID + META_ACCESS_TOKEN, "
                "THREADS_USER_ID + THREADS_ACCESS_TOKEN, or FACEBOOK_PAGE_ID + META_ACCESS_TOKEN"
            )

    # ------------------------------------------------------------------ setup

    def _target(self, platform: str) -> _Target:
        """Account id, token and host for one platform.

        Tokens come from the credential store rather than straight off settings,
        so a value the refresh job replaced is used on the next publish instead
        of the stale one this process booted with.
        """
        if platform == "instagram":
            if not settings.instagram_user_id:
                raise MetaError("INSTAGRAM_USER_ID is not set")
            # Instagram Login reaches the account directly; Facebook Login
            # reaches it through the Page it is linked to. Same endpoints
            # either way, different host and different token.
            instagram_token = credentials.get("INSTAGRAM_ACCESS_TOKEN")
            if instagram_token:
                return _Target(
                    platform,
                    str(settings.instagram_user_id),
                    instagram_token,
                    INSTAGRAM_HOST,
                )
            meta_token = credentials.get("META_ACCESS_TOKEN")
            if not meta_token:
                raise MetaError("neither INSTAGRAM_ACCESS_TOKEN nor META_ACCESS_TOKEN is set")
            return _Target(platform, str(settings.instagram_user_id), meta_token, FACEBOOK_HOST)

        if platform == "threads":
            threads_token = credentials.get("THREADS_ACCESS_TOKEN")
            if not (settings.threads_user_id and threads_token):
                raise MetaError("THREADS_USER_ID / THREADS_ACCESS_TOKEN are not set")
            return _Target(platform, str(settings.threads_user_id), threads_token, THREADS_HOST)

        if platform == "facebook":
            # The Page token carries no expiry, so it is never refreshed and
            # reads straight off settings.
            token = settings.facebook_page_token or credentials.get("META_ACCESS_TOKEN")
            if not (settings.facebook_page_id and token):
                raise MetaError("FACEBOOK_PAGE_ID / a page token are not set")
            return _Target(platform, str(settings.facebook_page_id), token, FACEBOOK_HOST)

        raise MetaError(f"{platform} is not a Meta platform")

    def _http(self) -> httpx.Client:
        if self._client is not None:
            return self._client
        return httpx.Client(timeout=httpx.Timeout(120.0, connect=30.0))

    # ------------------------------------------------------------- transport

    def _call(
        self,
        client: httpx.Client,
        method: str,
        url: str,
        *,
        data: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> dict:
        response = client.request(method, url, data=data, params=params, headers=headers)
        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 400 or "error" in payload:
            error = payload.get("error") or {}
            message = error.get("message") or response.text[:300]
            # Meta's subcode is the part that actually tells you what to fix.
            subcode = error.get("error_subcode")
            detail = f"{message} (code {error.get('code')}"
            detail += f"/{subcode})" if subcode else ")"
            raise MetaError(detail)
        return payload

    def _graph(self, target: _Target, path: str) -> str:
        return f"{target.host}/{self.version}/{path.lstrip('/')}"

    # --------------------------------------------------------------- helpers

    def _clip_url(self, request: PublishRequest) -> str:
        """A URL Meta's servers can fetch the clip from."""
        if request.public_url:
            return request.public_url
        if not request.storage_key:
            raise MetaError("no storage key on the request - nothing to hand Meta")
        storage = get_storage()
        if storage.kind != "r2":
            raise MetaError(
                "Meta downloads the video itself, so the clip must be on public storage. "
                "Configure R2 (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET)."
            )
        return storage.url_for(request.storage_key, expires_s=CLIP_URL_TTL_S)

    def _caption(self, request: PublishRequest, platform: str) -> str:
        return request.caption[: CAPTION_LIMITS[platform]]

    def _await_container(
        self,
        client: httpx.Client,
        target: _Target,
        container_id: str,
        *,
        may_be_silent: bool = False,
    ) -> None:
        """Block until Meta has finished ingesting the video, or give up.

        Publishing a container that is still IN_PROGRESS fails, so this is not
        optional politeness - it is the handshake.

        `may_be_silent` is for the carousel parent, which is assembled from
        children rather than downloaded and which some Graph versions do not
        report a status for at all. Polling a field that is never going to
        appear is not patience, it is a two-minute timeout - so when nothing
        answers, take that as done rather than as still working.
        """
        # Threads reports `status`; Instagram reports `status_code`. Ask for both
        # and read whichever comes back.
        fields = "status,status_code,error_message"
        deadline = time.monotonic() + self.timeout_s
        last = "UNKNOWN"

        while time.monotonic() < deadline:
            payload = self._call(
                client,
                "GET",
                self._graph(target, container_id),
                params={"fields": fields, "access_token": target.token},
            )
            last = payload.get("status_code") or payload.get("status") or "UNKNOWN"
            if last in ("FINISHED", "PUBLISHED"):
                return
            if last in ("ERROR", "EXPIRED"):
                reason = payload.get("error_message") or last
                raise MetaError(f"Meta rejected the upload: {reason}")
            if may_be_silent and "status_code" not in payload and "status" not in payload:
                log.info("meta: %s reports no status; treating it as ready", container_id)
                return
            self._sleep(POLL_INTERVAL_S)

        raise MetaError(
            f"Meta was still {last} after {self.timeout_s}s - "
            "raise META_PUBLISH_TIMEOUT_S or check the clip encodes to H.264/AAC"
        )

    def _publish_creation(
        self,
        client: httpx.Client,
        target: _Target,
        creation_id: str,
        path: str,
    ) -> str:
        """Publish a finished container, re-asking while Meta says it is not.

        Safe to repeat: a call that comes back "Media ID is not available"
        published nothing, so there is no way for this to put the same post up
        twice. The alternative - one try, one red pill - throws away a post
        over a few seconds of Meta catching up with itself.
        """
        last: MetaError | None = None
        for attempt in range(PUBLISH_ATTEMPTS):
            try:
                published = self._call(
                    client,
                    "POST",
                    self._graph(target, path),
                    data={"creation_id": creation_id, "access_token": target.token},
                )
            except MetaError as exc:
                if not is_not_ready(str(exc)):
                    raise
                last = exc
                log.info("meta: %s not ready yet (try %d)", creation_id, attempt + 1)
                if attempt < PUBLISH_ATTEMPTS - 1:
                    self._sleep(PUBLISH_BACKOFF_S)
                continue
            post_id = str(published.get("id") or "")
            if not post_id:
                raise MetaError(f"no post id in the response: {published}")
            return post_id

        raise MetaError(
            f"{last} - the container was still not publishable after "
            f"{PUBLISH_ATTEMPTS} tries"
        )

    def _permalink(self, client: httpx.Client, target: _Target, post_id: str) -> str | None:
        """Best effort - a missing permalink is not a failed post."""
        try:
            payload = self._call(
                client,
                "GET",
                self._graph(target, post_id),
                params={"fields": "permalink", "access_token": target.token},
            )
        except MetaError:
            return None
        return payload.get("permalink")

    # ------------------------------------------------------------ publishing

    def publish(self, request: PublishRequest) -> list[PublishResult]:
        wanted = [p for p in request.platforms if p in PLATFORMS]
        skipped = [p for p in request.platforms if p not in PLATFORMS]

        results = [
            PublishResult(platform=p, ok=False, error="not a Meta platform - use another publisher")
            for p in skipped
        ]
        if not wanted:
            return results or [
                PublishResult(platform="none", ok=False, error="no Meta platforms requested")
            ]

        try:
            clip_url = self._clip_url(request)
        except MetaError as exc:
            return results + [PublishResult(platform=p, ok=False, error=str(exc)) for p in wanted]

        client = self._http()
        owns_client = self._client is None
        try:
            for platform in wanted:
                results.append(self._publish_one(client, platform, request, clip_url))
        finally:
            if owns_client:
                client.close()
        return results

    def _publish_one(
        self,
        client: httpx.Client,
        platform: str,
        request: PublishRequest,
        clip_url: str,
    ) -> PublishResult:
        try:
            target = self._target(platform)
            if platform == "facebook":
                return self._publish_facebook_reel(client, target, request, clip_url)
            return self._publish_container(client, target, request, clip_url)
        except MetaError as exc:
            log.warning("meta: %s failed: %s", platform, exc)
            return PublishResult(platform=platform, ok=False, error=str(exc)[:300])
        except httpx.HTTPError as exc:
            log.warning("meta: %s request failed: %s", platform, exc)
            return PublishResult(platform=platform, ok=False, error=f"request failed: {exc}"[:300])

    # ------------------------------------------------------------- carousel

    def publish_carousel(
        self,
        image_url: str,
        video_url: str,
        caption: str,
    ) -> PublishResult:
        """An Instagram carousel: a cover image, then the video.

        A different flow from a reel and not a variation on one. Each slide
        gets its own container with is_carousel_item set, then a third
        container ties them together, then that is published - four calls
        where a reel takes two.

        The video slide is `VIDEO`, never `REELS`. A reel cannot be a carousel
        item, and asking for one is rejected in a way that reads like a bad
        URL rather than a wrong media type.
        """
        try:
            target = self._target("instagram")
        except MetaError as exc:
            return PublishResult(platform="instagram_carousel", ok=False, error=str(exc)[:300])

        client = self._http()
        owns_client = self._client is None
        try:
            try:
                children = []
                for body in (
                    {"is_carousel_item": "true", "image_url": image_url},
                    {"is_carousel_item": "true", "media_type": "VIDEO",
                     "video_url": video_url},
                ):
                    created = self._call(
                        client, "POST",
                        self._graph(target, f"{target.account_id}/media"),
                        data=body | {"access_token": target.token},
                    )
                    child = str(created.get("id") or "")
                    if not child:
                        raise MetaError(f"no container id for a slide: {created}")
                    children.append(child)

                # Every slide, not just the video. An image container is
                # usually ready in a second, but tying the carousel together
                # while any child is still being fetched produces a parent
                # that can never be published - and the refusal names the
                # parent, so the half-finished child never comes up.
                for child in children:
                    self._await_container(client, target, child)

                parent = self._call(
                    client, "POST",
                    self._graph(target, f"{target.account_id}/media"),
                    data={
                        "media_type": "CAROUSEL",
                        "children": ",".join(children),
                        "caption": caption[: CAPTION_LIMITS["instagram"]],
                        "access_token": target.token,
                    },
                )
                parent_id = str(parent.get("id") or "")
                if not parent_id:
                    raise MetaError(f"no carousel container id: {parent}")

                # The parent is a container as well. Publishing it the
                # instant it is created is what "Media ID is not available"
                # means - Meta hands back an id before it has finished
                # assembling the thing the id refers to.
                self._await_container(client, target, parent_id, may_be_silent=True)

                post_id = self._publish_creation(
                    client, target, parent_id, f"{target.account_id}/media_publish",
                )

                # Asked for inside the client block: the permalink is a
                # separate request, and a post that went out is still a post
                # that went out if looking up its URL fails.
                link = self._permalink(client, target, post_id)

            except MetaError as exc:
                log.warning("meta: carousel failed: %s", exc)
                return PublishResult(platform="instagram_carousel", ok=False,
                                     error=str(exc)[:300])
            except httpx.HTTPError as exc:
                log.warning("meta: carousel request failed: %s", exc)
                return PublishResult(platform="instagram_carousel", ok=False,
                                     error=f"request failed: {exc}"[:300])
        finally:
            if owns_client:
                client.close()

        return PublishResult(
            platform="instagram_carousel", ok=True, post_id=post_id, url=link,
        )

    def _publish_container(
        self,
        client: httpx.Client,
        target: _Target,
        request: PublishRequest,
        clip_url: str,
    ) -> PublishResult:
        """Instagram Reels and Threads: create container, wait, publish."""
        caption = self._caption(request, target.platform)

        if target.platform == "instagram":
            create_path = f"{target.account_id}/media"
            publish_path = f"{target.account_id}/media_publish"
            body = {
                "media_type": "REELS",
                "video_url": clip_url,
                "caption": caption,
                "access_token": target.token,
            }
        else:
            create_path = f"{target.account_id}/threads"
            publish_path = f"{target.account_id}/threads_publish"
            body = {
                "media_type": "VIDEO",
                "video_url": clip_url,
                "text": caption,
                "access_token": target.token,
            }

        created = self._call(client, "POST", self._graph(target, create_path), data=body)
        container_id = str(created.get("id") or "")
        if not container_id:
            raise MetaError(f"no container id in the response: {created}")

        self._await_container(client, target, container_id)

        post_id = self._publish_creation(client, target, container_id, publish_path)

        return PublishResult(
            platform=target.platform,
            ok=True,
            post_id=post_id,
            url=self._permalink(client, target, post_id),
        )

    def _publish_facebook_reel(
        self,
        client: httpx.Client,
        target: _Target,
        request: PublishRequest,
        clip_url: str,
    ) -> PublishResult:
        """Facebook Reels: start, transfer by URL, finish."""
        endpoint = self._graph(target, f"{target.account_id}/video_reels")

        started = self._call(
            client,
            "POST",
            endpoint,
            data={"upload_phase": "start", "access_token": target.token},
        )
        video_id = str(started.get("video_id") or "")
        upload_url = started.get("upload_url")
        if not video_id or not upload_url:
            raise MetaError(f"start phase returned nothing usable: {started}")

        # Transfer phase. `file_url` tells Meta to pull the file itself, which
        # avoids streaming the whole clip out of this container.
        self._call(
            client,
            "POST",
            upload_url,
            headers={"Authorization": f"OAuth {target.token}", "file_url": clip_url},
        )

        self._call(
            client,
            "POST",
            endpoint,
            data={
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "PUBLISHED",
                "description": self._caption(request, "facebook"),
                "access_token": target.token,
            },
        )

        return PublishResult(
            platform="facebook",
            ok=True,
            post_id=video_id,
            url=f"https://www.facebook.com/reel/{video_id}",
        )


# ------------------------------------------------------- who am I posting as

# What each platform calls the human-readable account name.
NAME_FIELD = {"instagram": "username", "threads": "username", "facebook": "name"}


def describe_accounts(client: httpx.Client | None = None) -> dict[str, str]:
    """Ask each platform which account the configured id actually is.

    An account id is eight digits of nothing, and having two Instagram accounts
    on one app means a single wrong digit posts to the other one. This resolves
    the ids to names so you can check before trusting the queue.
    """
    publisher = MetaPublisher(client=client)
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    out: dict[str, str] = {}
    try:
        for platform in PLATFORMS:
            try:
                target = publisher._target(platform)
            except MetaError:
                continue
            try:
                payload = publisher._call(
                    client,
                    "GET",
                    publisher._graph(target, target.account_id),
                    params={
                        "fields": f"id,{NAME_FIELD[platform]}",
                        "access_token": target.token,
                    },
                )
                out[platform] = str(payload.get(NAME_FIELD[platform]) or payload)
            except (MetaError, httpx.HTTPError) as exc:
                out[platform] = f"could not resolve: {exc}"
    finally:
        if owns_client:
            client.close()
    return out


# ---------------------------------------------------------------- token care

# Each platform swaps a short-lived token for a 60-day one at its own endpoint,
# with its own grant type. The dashboard hands you the short-lived kind.
EXCHANGE = {
    "instagram": (INSTAGRAM_HOST + "/access_token", "ig_exchange_token"),
    "threads": (THREADS_HOST + "/access_token", "th_exchange_token"),
}

# Instagram Login and Threads are separate apps under the same Meta app, each
# with its own secret on its own use case page. Signing the exchange with the
# Meta secret fails in a way that blames the token, so this is worth getting
# right rather than discovering.
_SECRET_VAR = {
    "instagram": "INSTAGRAM_APP_SECRET",
    "threads": "THREADS_APP_SECRET",
    "facebook": "META_APP_SECRET",
}


def _app_secret_for(platform: str) -> str | None:
    """The secret that signs this platform's exchange, falling back to Meta's."""
    if platform == "instagram":
        return settings.instagram_app_secret or settings.meta_app_secret
    if platform == "threads":
        return settings.threads_app_secret or settings.meta_app_secret
    return settings.meta_app_secret


def exchange_token(
    platform: str,
    short_lived: str,
    app_secret: str | None = None,
    client: httpx.Client | None = None,
) -> str:
    """Swap a token from the app dashboard for one that lasts 60 days.

    The generators in Meta's UI give you roughly an hour. Putting that straight
    into the environment produces a queue that works during setup and is broken
    by morning, which is a confusing way to lose a day.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    secret = app_secret or _app_secret_for(platform)
    if not secret:
        raise MetaError(f"{_SECRET_VAR.get(platform, 'META_APP_SECRET')} is not set")
    try:
        if platform == "facebook":
            if not settings.meta_app_id:
                raise MetaError("META_APP_ID is not set")
            response = client.get(
                f"{FACEBOOK_HOST}/{settings.meta_graph_version}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.meta_app_id,
                    "client_secret": secret,
                    "fb_exchange_token": short_lived,
                },
            )
        elif platform in EXCHANGE:
            url, grant = EXCHANGE[platform]
            response = client.get(
                url,
                params={
                    "grant_type": grant,
                    "client_secret": secret,
                    "access_token": short_lived,
                },
            )
        else:
            raise MetaError(f"{platform} does not exchange tokens")

        payload = response.json()
        token = payload.get("access_token")
        if not token:
            raise MetaError(_exchange_error(platform, payload))
        return str(token)
    finally:
        if owns_client:
            client.close()


def list_page_tokens(client: httpx.Client | None = None) -> list[dict[str, str]]:
    """Every Page the long-lived user token can post to, with its own token.

    Page tokens derived from a *long-lived* user token carry no expiry of their
    own, so this is the one credential in the setup that does not need a diary
    entry. Derive it from a short-lived user token and you get a short-lived
    Page token instead - so run the exchange first.
    """
    if not settings.meta_access_token:
        raise MetaError("META_ACCESS_TOKEN is not set - exchange a user token first")

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        response = client.get(
            f"{FACEBOOK_HOST}/{settings.meta_graph_version}/me/accounts",
            params={
                "fields": "id,name,access_token",
                "access_token": settings.meta_access_token,
            },
        )
        payload = response.json()
        if "error" in payload:
            raise MetaError(str(payload["error"].get("message", payload["error"])))
        return [
            {
                "id": str(page.get("id", "")),
                "name": str(page.get("name", "")),
                "access_token": str(page.get("access_token", "")),
            }
            for page in payload.get("data", [])
        ]
    finally:
        if owns_client:
            client.close()


def _exchange_error(platform: str, payload: dict) -> str:
    """Explain a refused exchange, especially the one that reads as a bad token.

    Meta's app dashboard hands out tokens that are *already* long-lived, and
    exchanging one fails with "the type of access token is invalid" - which
    reads as a broken token and sends you back to regenerate it, forever. Say
    what is actually wrong instead.
    """
    error = payload.get("error") or {}
    subcode = error.get("error_subcode")
    code = error.get("code")
    if subcode == 2207055 or code == 452:
        return (
            "this token cannot be exchanged, which usually means it is already "
            "long-lived - tokens generated in the app dashboard last 60 days "
            f"and should be used as-is. Set the {platform.upper()} token "
            "straight from the dashboard and skip the exchange."
        )
    return f"exchange refused: {error or payload}"


def refresh_tokens(client: httpx.Client | None = None) -> dict[str, str]:
    """Extend the long-lived tokens by another 60 days.

    Meta will not refresh a token that is already expired, so this has to run
    before the two months are up. Returns the new values to put back in the
    environment - it cannot write them there itself.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    fresh: dict[str, str] = {}
    try:
        if settings.meta_access_token and settings.meta_app_id and settings.meta_app_secret:
            response = client.get(
                f"{FACEBOOK_HOST}/{settings.meta_graph_version}/oauth/access_token",
                params={
                    "grant_type": "fb_exchange_token",
                    "client_id": settings.meta_app_id,
                    "client_secret": settings.meta_app_secret,
                    "fb_exchange_token": settings.meta_access_token,
                },
            )
            payload = response.json()
            if token := payload.get("access_token"):
                fresh["META_ACCESS_TOKEN"] = token
            else:
                log.warning("could not refresh the Meta token: %s", payload)

        if settings.instagram_access_token:
            response = client.get(
                f"{INSTAGRAM_HOST}/refresh_access_token",
                params={
                    "grant_type": "ig_refresh_token",
                    "access_token": settings.instagram_access_token,
                },
            )
            payload = response.json()
            if token := payload.get("access_token"):
                fresh["INSTAGRAM_ACCESS_TOKEN"] = token
            else:
                log.warning("could not refresh the Instagram token: %s", payload)

        if settings.threads_access_token:
            response = client.get(
                f"{THREADS_HOST}/refresh_access_token",
                params={
                    "grant_type": "th_refresh_token",
                    "access_token": settings.threads_access_token,
                },
            )
            payload = response.json()
            if token := payload.get("access_token"):
                fresh["THREADS_ACCESS_TOKEN"] = token
            else:
                log.warning("could not refresh the Threads token: %s", payload)
    finally:
        if owns_client:
            client.close()
    return fresh


def publishing_quota(client: httpx.Client | None = None) -> dict[str, object]:
    """How much of Instagram's 24-hour publishing allowance is spent.

    Meta reports this directly rather than making you count, which matters
    because the allowance is per account and a failed attempt can consume it
    without anything being published. Asked for by the dashboard so the answer
    to "why is nothing posting" can be a number rather than a guess.
    """
    if not settings.has_instagram:
        return {"known": False, "why": "Instagram is not configured"}

    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        publisher = MetaPublisher()
        target = publisher._target("instagram")
        response = client.get(
            publisher._graph(target, f"{target.account_id}/content_publishing_limit"),
            params={"fields": "config,quota_usage", "access_token": target.token},
        )
        payload = response.json()
    except Exception as exc:  # noqa: BLE001 - a readout must not raise
        return {"known": False, "why": f"{type(exc).__name__}: {exc}"[:200]}
    finally:
        if owns_client:
            client.close()

    rows = payload.get("data") or []
    if not rows:
        return {"known": False, "why": str(payload)[:200]}

    row = rows[0]
    config = row.get("config") or {}
    return {
        "known": True,
        "used": row.get("quota_usage"),
        "limit": config.get("quota_total"),
        "window_hours": (config.get("quota_duration") or 86400) // 3600,
    }
