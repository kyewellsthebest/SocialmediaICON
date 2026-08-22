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
        },
    }


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
