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

    # publishing (Phase 3)
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_refresh_token: str | None = None
    ig_app_id: str | None = None
    ig_app_secret: str | None = None
    ig_token: str | None = None

    # pipeline defaults
    default_niche: str = "general"
    top_n_clips: int = 3
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
    def transcription_key(self) -> str | None:
        if self.transcribe_provider == "deepgram":
            return self.deepgram_api_key
        return self.assemblyai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
