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

    # yt-dlp. YouTube challenges datacenter IPs, so which player client is
    # used matters, and the set that passes changes every few months -
    # hence a list to try in order rather than a value in the code.
    ytdlp_player_clients: str = "tv,web_safari,mweb,android_vr"
    ytdlp_cookies: str | None = None  # cookies.txt contents, pasted as-is
    ytdlp_cookies_b64: str | None = None  # or base64, if the text gets mangled
    ytdlp_cookiefile: str | None = None  # or a path, for local runs
    ytdlp_proxy: str | None = None  # residential proxy, if you have one
    # Accept formats that would normally be skipped for lacking a proof-of-
    # origin token. They can be selected but often 403 when actually fetched,
    # so this is off by default: a client offering nothing is a clean failure
    # that falls through to the next one, while a client offering something
    # unfetchable wastes the whole attempt. Only worth enabling with no proxy,
    # where a doomed attempt still beats no attempt.
    ytdlp_allow_missing_pot: bool = False
    # Residential proxies bill per gigabyte, and video is heavy: a 12 minute
    # 1080p source is 200-400 MB, 720p roughly half that. Since the render
    # crops 16:9 to 9:16 and upscales either way, this is the dial between
    # a sharper clip and a smaller bill.
    ingest_max_height: int = 1080
    # auto  - the worker downloads the video itself (needs an IP YouTube
    #         will serve, so a proxy or a lucky host)
    # agent - the source waits for scripts/local_agent.py to supply the file
    #         from a machine with an ordinary home connection
    ingest_mode: str = "auto"

    # publishing
    publisher: str = "manual"  # manual | upload_post | youtube | meta
    upload_post_api_key: str | None = None
    upload_post_user: str | None = None
    upload_post_base_url: str = "https://api.upload-post.com"

    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_refresh_token: str | None = None

    # Meta — Instagram Reels, Facebook Reels, Threads.
    # Pin the Graph version: Meta retires each one roughly two years after
    # release, and an unpinned call silently follows whatever is current.
    meta_graph_version: str = "v23.0"
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_access_token: str | None = None  # long-lived user or page token
    instagram_user_id: str | None = None  # the IG *business* account id
    # Set this to use Instagram Login instead of Facebook Login. That route
    # talks to graph.instagram.com and does not care which Page the account
    # is linked to - the way out when the Page is tied to a different
    # Instagram account than the one you post from.
    instagram_access_token: str | None = None
    # Instagram Login and Threads each issue their own app id/secret pair,
    # shown on their own use case page. The exchange is signed with those,
    # not with the Meta app secret - a detail that costs an evening to find
    # because the error it produces names the token, not the secret.
    instagram_app_id: str | None = None
    instagram_app_secret: str | None = None
    facebook_page_id: str | None = None
    facebook_page_token: str | None = None  # falls back to meta_access_token
    threads_user_id: str | None = None
    threads_access_token: str | None = None  # a separate token from the FB one
    threads_app_id: str | None = None
    threads_app_secret: str | None = None
    # Meta downloads and transcodes the file itself; a 60s 1080x1920 clip is
    # usually done inside a minute, but the queue is shared and can be slow.
    meta_publish_timeout_s: int = 420
    # A quarter of the 60-day token life: three missed runs still leave a
    # fortnight before anything lapses.
    token_refresh_interval_days: int = 14

    # dashboard access (the app is public on Railway unless this is set)
    dashboard_token: str | None = None

    # automation cadence
    scout_enabled: bool = True
    scout_interval_minutes: int = 360  # every 6h - hourly is a waste of quota
    scout_keywords: str = ""  # comma separated, per niche
    scout_region: str | None = None
    scout_video_duration: str = "medium"  # short | medium | long
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
    def ingest_by_agent(self) -> bool:
        return self.ingest_mode.strip().lower() == "agent"

    @property
    def has_instagram(self) -> bool:
        return bool(
            self.instagram_user_id and (self.instagram_access_token or self.meta_access_token)
        )

    @property
    def instagram_via_instagram_login(self) -> bool:
        """True when Instagram is reached directly rather than through a Page."""
        return bool(self.instagram_access_token)

    @property
    def has_facebook(self) -> bool:
        return bool(self.facebook_page_id and (self.facebook_page_token or self.meta_access_token))

    @property
    def has_threads(self) -> bool:
        return bool(self.threads_user_id and self.threads_access_token)

    @property
    def has_meta_tokens(self) -> bool:
        return bool(
            self.meta_access_token or self.instagram_access_token or self.threads_access_token
        )

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
