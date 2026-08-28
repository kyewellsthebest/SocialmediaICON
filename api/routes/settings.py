"""Runtime settings.

Secrets stay in the environment - this endpoint never returns a key. What it
does expose is the operating configuration (which niche, which keywords, how
often, how many posts a day) and lets the dashboard edit the parts that live in
the database rather than in env vars.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core import credentials, ytdlp
from core.config import settings as env_settings
from core.db import get_db
from core.models import Account, Niche

router = APIRouter(prefix="/settings", tags=["settings"])


class NicheIn(BaseModel):
    name: str
    keywords: list[str] = []
    cadence_per_day: int | None = None
    caption_style: str | None = None


class AccountIn(BaseModel):
    platform: str
    handle: str
    status: str = "active"


def _niche_payload(niche: Niche) -> dict[str, Any]:
    config = niche.config or {}
    return {
        "id": niche.id,
        "name": niche.name,
        "keywords": config.get("keywords", []),
        "cadence_per_day": config.get("cadence_per_day"),
        "caption_style": config.get("caption_style"),
    }


@router.get("")
def read_settings(db: Session = Depends(get_db)) -> dict[str, Any]:
    niches = [_niche_payload(n) for n in db.query(Niche).order_by(Niche.name).all()]
    accounts = [
        {"id": a.id, "platform": a.platform, "handle": a.handle, "status": a.status}
        for a in db.query(Account).order_by(Account.platform).all()
    ]
    return {
        "niches": niches,
        "accounts": accounts,
        "env": {
            "env": env_settings.env,
            "default_niche": env_settings.default_niche,
            "scout_enabled": env_settings.scout_enabled,
            "scout_interval_minutes": env_settings.scout_interval_minutes,
            "scout_keywords": env_settings.keywords,
            "scout_video_duration": env_settings.scout_video_duration,
            "metrics_interval_minutes": env_settings.metrics_interval_minutes,
            "autopost_enabled": env_settings.autopost_enabled,
            "autopost_per_day": env_settings.autopost_per_day,
            "publisher": env_settings.publisher,
            "top_n_clips": env_settings.top_n_clips,
            "clip_length_s": [env_settings.min_clip_s, env_settings.max_clip_s],
            "transcribe_provider": env_settings.transcribe_provider,
            "model": env_settings.anthropic_model,
        },
        "connected": {
            "database": env_settings.has_db,
            "redis": env_settings.has_redis,
            "r2": env_settings.has_r2,
            "anthropic": bool(env_settings.anthropic_api_key),
            "transcription": bool(env_settings.transcription_key),
            "youtube_read": env_settings.has_youtube_read,
            "youtube_upload": bool(env_settings.youtube_refresh_token),
            "upload_post": bool(env_settings.upload_post_api_key),
            "instagram": env_settings.has_instagram,
            "threads": env_settings.has_threads,
            "facebook": env_settings.has_facebook,
        },
    }


@router.get("/meta")
def meta_status() -> dict[str, Any]:
    """Which Meta accounts the configured ids actually resolve to.

    Setup is a chain of ids and tokens that all look alike, and the failure this
    catches is posting to the wrong Instagram account - which nothing else will
    tell you until it has already happened. So this asks Meta to name each
    account rather than reporting that a variable is non-empty.

    Never returns a token, only what one resolves to.
    """
    configured = {
        "instagram": env_settings.has_instagram,
        "threads": env_settings.has_threads,
        "facebook": env_settings.has_facebook,
    }
    # Naming the empty variables turns "it does not work" into a checklist. The
    # usual cause is a shared variable that was never applied to this service,
    # and that looks identical to a typo until you can see which name is blank.
    present = {
        "META_APP_ID": bool(env_settings.meta_app_id),
        "META_APP_SECRET": bool(env_settings.meta_app_secret),
        "META_ACCESS_TOKEN": bool(env_settings.meta_access_token),
        "INSTAGRAM_USER_ID": bool(env_settings.instagram_user_id),
        "INSTAGRAM_ACCESS_TOKEN": bool(env_settings.instagram_access_token),
        "THREADS_USER_ID": bool(env_settings.threads_user_id),
        "THREADS_ACCESS_TOKEN": bool(env_settings.threads_access_token),
        "FACEBOOK_PAGE_ID": bool(env_settings.facebook_page_id),
        "FACEBOOK_PAGE_TOKEN": bool(env_settings.facebook_page_token),
        "R2_ACCOUNT_ID": bool(env_settings.r2_account_id),
        "R2_ACCESS_KEY_ID": bool(env_settings.r2_access_key_id),
        "R2_SECRET_ACCESS_KEY": bool(env_settings.r2_secret_access_key),
        "R2_BUCKET": bool(env_settings.r2_bucket),
    }
    payload: dict[str, Any] = {
        "publisher": env_settings.publisher,
        "graph_version": env_settings.meta_graph_version,
        "instagram_route": (
            "instagram_login" if env_settings.instagram_via_instagram_login else "facebook_login"
        ),
        "configured": configured,
        # Meta fetches the clip from a URL, so publishing needs public storage.
        "storage_ready": env_settings.has_r2,
        "accounts": {},
        "tokens": [],
        "variables_set": sorted(name for name, ok in present.items() if ok),
        "variables_missing": sorted(name for name, ok in present.items() if not ok),
    }

    if not any(configured.values()):
        payload["hint"] = (
            "No Meta credentials reached this service. If you set them as Railway "
            "shared variables, they must also be applied to each service - check "
            "the web service's own Variables tab, then redeploy."
        )
        return payload

    from core.publishers.meta import describe_accounts

    try:
        payload["accounts"] = describe_accounts()
    except Exception as exc:  # noqa: BLE001 - a dead token must not 500 the page
        payload["accounts"] = {}
        payload["error"] = str(exc)[:300]

    payload["tokens"] = [
        {
            "name": row["name"],
            "days_left": row["days_left"],
            "refreshed_at": row["refreshed_at"].isoformat() if row["refreshed_at"] else None,
            "last_error": row["last_error"],
        }
        for row in credentials.status()
    ]
    if not env_settings.has_r2:
        payload["hint"] = (
            "Meta downloads the clip from a URL, so R2 must be configured before "
            "PUBLISHER=meta can post."
        )
    return payload


@router.get("/ytdlp")
def ytdlp_status() -> dict[str, Any]:
    """Whether the download config actually reached this service.

    Note this reports the *web* service's view. Downloads run in the worker, so
    a variable shared with one and not the other will read as fine here and
    still fail there - share with all three.
    """
    payload = ytdlp.describe()
    # Impersonation first: without it Kick is unreachable outright, which is a
    # harder failure than any cookie problem and is fixed by a dependency
    # rather than by anything the user has to go and fetch.
    if not payload["impersonation"]["available"]:
        payload["hint"] = (
            "Browser TLS impersonation is unavailable, so Kick will answer 403 "
            "to everything - Cloudflare rejects Python's handshake before it "
            "reads the URL. Install curl_cffi (it is in pyproject; redeploy "
            "if this service predates it)."
        )
    elif not payload["cookies_loaded"]:
        payload["hint"] = (
            "No cookies loaded. Paste the contents of a cookies.txt into "
            "YTDLP_COOKIES and share it with web, worker and scheduler."
        )
    elif payload["cookie_lines"] < 5:
        payload["hint"] = (
            f"Only {payload['cookie_lines']} cookie lines were read - an export "
            "from a logged-in YouTube session usually has dozens. The paste may "
            "be partial."
        )
    return payload


@router.post("/niches")
def upsert_niche(payload: NicheIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    niche = db.query(Niche).filter(Niche.name == payload.name).one_or_none()
    if niche is None:
        niche = Niche(name=payload.name, config={})
        db.add(niche)
        db.flush()

    config = dict(niche.config or {})
    config["keywords"] = payload.keywords
    if payload.cadence_per_day is not None:
        config["cadence_per_day"] = payload.cadence_per_day
    if payload.caption_style is not None:
        config["caption_style"] = payload.caption_style
    niche.config = config
    db.flush()
    return _niche_payload(niche)


@router.post("/accounts")
def add_account(payload: AccountIn, db: Session = Depends(get_db)) -> dict[str, Any]:
    account = (
        db.query(Account)
        .filter(Account.platform == payload.platform, Account.handle == payload.handle)
        .one_or_none()
    )
    if account is None:
        account = Account(platform=payload.platform, handle=payload.handle)
        db.add(account)
    account.status = payload.status
    db.flush()
    return {
        "id": account.id,
        "platform": account.platform,
        "handle": account.handle,
        "status": account.status,
    }


@router.delete("/accounts/{account_id}")
def remove_account(account_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    account = db.get(Account, account_id)
    if account is not None:
        db.delete(account)
    return {"deleted": account_id}
