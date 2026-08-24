"""The Meta publisher, driven against a fake Graph API.

These tests pin the call *sequence*, because that is what Meta actually
enforces: a container published before it finished transcoding is rejected, and
a Facebook Reel finished before the transfer phase is a dead video id.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from core.config import settings
from core.publishers import PublishRequest
from core.publishers.meta import MetaError, MetaPublisher


@pytest.fixture
def meta_env(monkeypatch):
    monkeypatch.setattr(settings, "meta_graph_version", "v23.0", raising=False)
    monkeypatch.setattr(settings, "meta_access_token", "fb-token", raising=False)
    monkeypatch.setattr(settings, "instagram_user_id", "17841400000000000", raising=False)
    monkeypatch.setattr(settings, "facebook_page_id", "1122334455", raising=False)
    monkeypatch.setattr(settings, "facebook_page_token", None, raising=False)
    monkeypatch.setattr(settings, "threads_user_id", "98765", raising=False)
    monkeypatch.setattr(settings, "threads_access_token", "th-token", raising=False)
    monkeypatch.setattr(settings, "meta_publish_timeout_s", 30, raising=False)
    return settings


def _request(platforms: list[str]) -> PublishRequest:
    return PublishRequest(
        clip_path=Path("clip.mp4"),
        title="Found a nugget in the shallows",
        description="Found a nugget in the shallows",
        hashtags=["#goldprospecting", "#panning"],
        platforms=platforms,
        public_url="https://example.invalid/clip.mp4",
    )


def _publisher(handler, meta_env) -> MetaPublisher:
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return MetaPublisher(client=client, sleep=lambda _s: None)


def test_instagram_reel_creates_waits_then_publishes(meta_env):
    calls: list[str] = []
    statuses = iter(["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(f"{request.method} {path}")
        if path.endswith("/media"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["media_type"] == "REELS"
            assert body["video_url"] == "https://example.invalid/clip.mp4"
            assert "#goldprospecting" in body["caption"]
            return httpx.Response(200, json={"id": "container-1"})
        if path.endswith("/container-1"):
            return httpx.Response(200, json={"status_code": next(statuses)})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "media-9"})
        if path.endswith("/media-9"):
            return httpx.Response(200, json={"permalink": "https://instagram.com/reel/abc"})
        raise AssertionError(f"unexpected call: {path}")

    results = _publisher(handler, meta_env).publish(_request(["instagram"]))

    assert [r.ok for r in results] == [True]
    assert results[0].post_id == "media-9"
    assert results[0].url == "https://instagram.com/reel/abc"
    # Polled until FINISHED, and only then published.
    assert calls.count("GET /v23.0/container-1") == 3
    assert calls.index("POST /v23.0/17841400000000000/media_publish") > calls.index(
        "GET /v23.0/container-1"
    )


def test_threads_uses_its_own_host_and_field_names(meta_env):
    hosts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        path = request.url.path
        if path.endswith("/threads"):
            body = dict(httpx.QueryParams(request.content.decode()))
            assert body["media_type"] == "VIDEO"
            assert body["text"]  # Threads calls it text, not caption
            assert body["access_token"] == "th-token"
            return httpx.Response(200, json={"id": "th-container"})
        if path.endswith("/th-container"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "th-post"})
        return httpx.Response(200, json={"permalink": "https://threads.net/p/1"})

    results = _publisher(handler, meta_env).publish(_request(["threads"]))

    assert results[0].ok is True
    assert results[0].post_id == "th-post"
    assert set(hosts) == {"graph.threads.net"}


def test_threads_caption_is_cut_to_the_500_char_limit(meta_env):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/threads"):
            seen.update(dict(httpx.QueryParams(request.content.decode())))
            return httpx.Response(200, json={"id": "c"})
        if path.endswith("/c"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "p"})
        return httpx.Response(200, json={})

    request = _request(["threads"])
    request.description = "x" * 900
    _publisher(handler, meta_env).publish(request)

    assert len(seen["text"]) == 500


def test_facebook_reel_runs_start_transfer_finish_in_order(meta_env):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "rupload" in request.url.host:
            calls.append("transfer")
            assert request.headers["authorization"] == "OAuth fb-token"
            assert request.headers["file_url"] == "https://example.invalid/clip.mp4"
            return httpx.Response(200, json={"success": True})
        body = dict(httpx.QueryParams(request.content.decode()))
        calls.append(body["upload_phase"])
        if body["upload_phase"] == "start":
            return httpx.Response(
                200,
                json={
                    "video_id": "vid-7",
                    "upload_url": "https://rupload.facebook.com/video-upload/v23.0/vid-7",
                },
            )
        assert body["video_id"] == "vid-7"
        assert body["video_state"] == "PUBLISHED"
        return httpx.Response(200, json={"success": True})

    results = _publisher(handler, meta_env).publish(_request(["facebook"]))

    assert calls == ["start", "transfer", "finish"]
    assert results[0].ok is True
    assert results[0].url == "https://www.facebook.com/reel/vid-7"


def test_a_failing_platform_does_not_take_down_the_others(meta_env):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/media"):
            return httpx.Response(
                400,
                json={"error": {"message": "The account is not a business account", "code": 10}},
            )
        if path.endswith("/threads"):
            return httpx.Response(200, json={"id": "c"})
        if path.endswith("/c"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "p"})
        return httpx.Response(200, json={})

    results = _publisher(handler, meta_env).publish(_request(["instagram", "threads"]))
    outcome = {r.platform: r for r in results}

    assert outcome["instagram"].ok is False
    assert "business account" in outcome["instagram"].error
    assert outcome["threads"].ok is True


def test_a_container_error_is_reported_not_published(meta_env):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/media"):
            return httpx.Response(200, json={"id": "c"})
        if path.endswith("/c"):
            return httpx.Response(
                200, json={"status_code": "ERROR", "error_message": "video is not 3:4 to 1.91:1"}
            )
        raise AssertionError("must not publish a container that errored")

    results = _publisher(handler, meta_env).publish(_request(["instagram"]))

    assert results[0].ok is False
    assert "not 3:4" in results[0].error


def test_polling_gives_up_rather_than_hanging(meta_env, monkeypatch):
    monkeypatch.setattr(settings, "meta_publish_timeout_s", 0, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"id": "c"})
        return httpx.Response(200, json={"status_code": "IN_PROGRESS"})

    results = _publisher(handler, meta_env).publish(_request(["instagram"]))

    assert results[0].ok is False
    assert "IN_PROGRESS" in results[0].error or "still" in results[0].error


def test_non_meta_platforms_are_reported_not_silently_dropped(meta_env):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/threads"):
            return httpx.Response(200, json={"id": "c"})
        if path.endswith("/c"):
            return httpx.Response(200, json={"status": "FINISHED"})
        if path.endswith("/threads_publish"):
            return httpx.Response(200, json={"id": "p"})
        return httpx.Response(200, json={})

    results = _publisher(handler, meta_env).publish(_request(["tiktok", "threads"]))
    outcome = {r.platform: r for r in results}

    assert outcome["tiktok"].ok is False
    assert "not a Meta platform" in outcome["tiktok"].error
    assert outcome["threads"].ok is True


def test_local_storage_is_refused_with_an_actionable_message(meta_env, monkeypatch):
    monkeypatch.setattr(settings, "r2_bucket", None, raising=False)

    request = _request(["instagram"])
    request.public_url = None
    request.storage_key = "clips/1.mp4"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call Meta without a fetchable URL")

    results = _publisher(handler, meta_env).publish(request)

    assert results[0].ok is False
    assert "R2" in results[0].error


def test_construction_fails_loudly_when_nothing_is_configured(monkeypatch):
    for key in ("instagram_user_id", "threads_user_id", "facebook_page_id"):
        monkeypatch.setattr(settings, key, None, raising=False)

    with pytest.raises(RuntimeError, match="no Meta account is configured"):
        MetaPublisher()


def test_target_selection_prefers_the_page_token(meta_env, monkeypatch):
    monkeypatch.setattr(settings, "facebook_page_token", "page-token", raising=False)
    publisher = MetaPublisher(client=httpx.Client(), sleep=lambda _s: None)

    assert publisher._target("facebook").token == "page-token"
    assert publisher._target("instagram").token == "fb-token"
    with pytest.raises(MetaError):
        publisher._target("tiktok")


def test_instagram_login_uses_its_own_host_and_token(meta_env, monkeypatch):
    """The way out when the Page is linked to a different Instagram account."""
    monkeypatch.setattr(settings, "instagram_access_token", "ig-token", raising=False)
    hosts: list[str] = []
    tokens: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hosts.append(request.url.host)
        path = request.url.path
        if path.endswith("/media"):
            body = dict(httpx.QueryParams(request.content.decode()))
            tokens.append(body["access_token"])
            return httpx.Response(200, json={"id": "c"})
        if path.endswith("/c"):
            return httpx.Response(200, json={"status_code": "FINISHED"})
        if path.endswith("/media_publish"):
            return httpx.Response(200, json={"id": "p"})
        return httpx.Response(200, json={"permalink": "https://instagram.com/reel/x"})

    results = _publisher(handler, meta_env).publish(_request(["instagram"]))

    assert results[0].ok is True
    assert set(hosts) == {"graph.instagram.com"}
    assert tokens == ["ig-token"]


def test_facebook_login_is_still_the_default(meta_env, monkeypatch):
    monkeypatch.setattr(settings, "instagram_access_token", None, raising=False)
    publisher = MetaPublisher(client=httpx.Client(), sleep=lambda _s: None)
    target = publisher._target("instagram")

    assert target.host == "https://graph.facebook.com"
    assert target.token == "fb-token"


def test_describe_accounts_names_every_configured_account(meta_env, monkeypatch):
    monkeypatch.setattr(settings, "instagram_access_token", "ig-token", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "graph.instagram.com":
            return httpx.Response(200, json={"id": "1", "username": "correct_account"})
        if request.url.host == "graph.threads.net":
            return httpx.Response(200, json={"id": "2", "username": "threads_handle"})
        return httpx.Response(200, json={"id": "3", "name": "The Page"})

    from core.publishers.meta import describe_accounts

    who = describe_accounts(client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert who == {
        "instagram": "correct_account",
        "threads": "threads_handle",
        "facebook": "The Page",
    }


def test_describe_accounts_reports_a_bad_id_instead_of_raising(meta_env):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"message": "Unsupported get request", "code": 100}}
        )

    from core.publishers.meta import describe_accounts

    who = describe_accounts(client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert all("could not resolve" in v for v in who.values())
