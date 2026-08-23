"""Environment configuration.

Everything is optional so that the Phase 1 CLI can run end-to-end with nothing
but an Anthropic key and a transcription key: no Postgres, no Redis, no R2.
Each subsystem exposes an `is_configured` style property that callers check
before reaching for it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    env: str = "dev"

    # infrastructure
    database_url: str | None = None
    redis_url: str | None = None

    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None

    # models / providers
    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-opus-5"
    anthropic_effort: str | None = None

    transcribe_provider: str = "assemblyai"
    assemblyai_api_key: str | None = None
    deepgram_api_key: str | None = None

    # trend scouting
    youtube_api_key: str | None = None

    # publishing
    publisher: str = "manual"  # manual | upload_post | youtube
    upload_post_api_key: str | None = None
    upload_post_user: str | None = None
    upload_post_base_url: str = "https://api.upload-post.com"

    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_refresh_token: str | None = None
    ig_app_id: str | None = None
    ig_app_secret: str | None = None
    ig_token: str | None = None

    # dashboard access (the app is public on Railway unless this is set)
    dashboard_token: str | None = None

    # automation cadence
    scout_enabled: bool = True
    scout_interval_minutes: int = 360      # every 6h - hourly is a waste of quota
    scout_keywords: str = ""               # comma separated, per niche
    scout_region: str | None = None
    scout_video_duration: str = "medium"   # short | medium | long
    scout_max_keywords: int = 4
    scout_track_limit: int = 30

    metrics_interval_minutes: int = 60
    autopost_enabled: bool = False
    autopost_per_day: int = 10

    # pipeline defaults
    default_niche: str = "general"
    top_n_clips: int = 3
    # Platforms stopped rewarding hashtag walls; a handful of specific tags
    # outperforms thirty broad ones and does not read as automated.
    hashtag_count: int = 4
    min_clip_s: float = 15.0
    max_clip_s: float = 60.0
    window_minutes: int = 6

    # local working dirs (used when R2 is not configured)
    local_storage_dir: Path = REPO_ROOT / ".storage"
    work_dir: Path = REPO_ROOT / ".work"

    @property
    def is_prod(self) -> bool:
        return self.env.lower() in {"prod", "production"}

    @property
    def has_db(self) -> bool:
        return bool(self.database_url)

    @property
    def has_redis(self) -> bool:
        return bool(self.redis_url)

    @property
    def has_r2(self) -> bool:
        return all(
            [self.r2_account_id, self.r2_access_key_id, self.r2_secret_access_key, self.r2_bucket]
        )

    @property
    def sqlalchemy_url(self) -> str:
        """Normalise a Railway/Heroku style URL onto the psycopg 3 driver."""
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        if url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://") :]
        return url

    @property
    def r2_endpoint_url(self) -> str:
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @property
    def has_youtube_read(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def keywords(self) -> list[str]:
        return [k.strip() for k in self.scout_keywords.split(",") if k.strip()]

    @property
    def transcription_key(self) -> str | None:
        if self.transcribe_provider == "deepgram":
            return self.deepgram_api_key
        return self.assemblyai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
