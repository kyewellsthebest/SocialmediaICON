"""Environment configuration.

Everything is optional so that the Phase 1 CLI can run end-to-end with nothing
but an Anthropic key and a transcription key: no Postgres, no Redis, no R2.
Each subsystem exposes an `is_configured` style property that callers check
before reaching for it.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import model_validator
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

    # Reddit. Free, answers from a datacenter, and its search is site-wide -
    # one query reaches every subreddit rather than one at a time.
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "clip-engine/0.1 (scout)"
    reddit_keywords: str = ""  # comma separated; blank reuses SCOUT_KEYWORDS
    # A 20-second clip of an already-short clip is not worth a render.
    reddit_min_duration_s: float = 45.0
    reddit_min_upvotes: int = 500
    reddit_time_filter: str = "month"  # hour|day|week|month|year|all
    # Reddit blocks unauthenticated reads from datacenter ranges. Credentials
    # are the better answer; this is the fallback, and defaults to reusing
    # the downloader's proxy pool.
    reddit_proxy: str | None = None

    # yt-dlp. YouTube challenges datacenter IPs, so which player client is
    # used matters, and the set that passes changes every few months -
    # hence a list to try in order rather than a value in the code.
    ytdlp_player_clients: str = "tv,web_safari,mweb,android_vr"
    ytdlp_cookies: str | None = None  # cookies.txt contents, pasted as-is
    ytdlp_cookies_b64: str | None = None  # or base64, if the text gets mangled
    ytdlp_cookiefile: str | None = None  # or a path, for local runs
    ytdlp_proxy: str | None = None  # residential proxy, if you have one
    # Several, tried in turn. Providers sell IPs in blocks and they are
    # shared, so they are not equally burned - one being challenged says
    # nothing about the next. Accepts full URLs or the ip:port:user:pass
    # lines proxy dashboards export, separated by commas or newlines.
    ytdlp_proxies: str | None = None
    # How many to try before giving up on a source. Twenty proxies times
    # five clients is a hundred attempts and several minutes; a handful is
    # enough to tell a burned IP from a burned block.
    ytdlp_max_proxies_per_run: int = 4
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

    # --- studio: original content from public-record audio ---------------
    # The studio makes videos rather than clipping them: real archive audio,
    # an AI narrator over the top, stock footage underneath, and a drawn
    # instrument overlay that never changes. Nothing here is needed by the
    # clip pipeline, so every key is optional and the studio reports what is
    # missing rather than failing at render time.

    # Narration. gpt-4o-mini-tts bills per minute of audio, not per character,
    # and takes a plain-English `instructions` field - which is what stops it
    # sounding like an assistant reading a script.
    openai_api_key: str | None = None
    # openai | elevenlabs. ElevenLabs is the better voice and costs about four
    # pounds a month more at ten videos a day; OpenAI is the cheaper default
    # and takes a plain-English instructions field.
    tts_provider: str = "openai"
    tts_model: str = "gpt-4o-mini-tts"
    tts_voice: str = "onyx"

    # ElevenLabs. The voice is named rather than an id: ids are opaque and
    # change per account, and the client resolves a name against /v1/voices.
    elevenlabs_api_key: str | None = None
    elevenlabs_voice: str = "Adam"
    # v3 is the best and dearest; turbo is half the price and close enough for
    # narration that sits under a recording.
    elevenlabs_model: str = "eleven_multilingual_v2"
    tts_instructions: str = (
        "Male, low register. Documentary narration. Measured and unhurried, "
        "slightly weary. Do not sound impressed by what you are saying. Fall "
        "in pitch at the end of every sentence. Leave a beat before the final "
        "clause."
    )

    # Stock footage. Pexels is free, has a documented API, and licenses for
    # commercial use with no attribution - the only stock source that
    # automates cleanly. 200 requests an hour is far more than this needs.
    pexels_api_key: str | None = None
    # Clips are cached by search term so the same twenty downloads serve
    # hundreds of renders. Raising this buys variety at the cost of disk.
    stock_cache_size: int = 40

    # Render shape. 24fps is deliberate: it is a third fewer overlay frames
    # to draw than 30 and reads as film rather than video.
    # How much of a long recording to transcribe looking for a moment. Half an
    # hour of mono 16 kHz mp3 is about 14 MB, inside OpenAI's 25 MB limit, and
    # costs roughly a penny to transcribe.
    studio_scan_minutes: float = 25.0
    studio_fps: int = 24
    studio_crf: int = 20
    # How hard the footage is pushed into the source's world, 0..1. At 0 the
    # stock clip shows through untouched and looks like stock; at 1 it is
    # crushed far enough that two clips from different shoots match.
    studio_grade: float = 0.88
    # How opaque the drawn instrument layer sits over it, 0..1.
    studio_overlay: float = 0.62
    # Approve-before-post. While this is on, a finished render waits in the
    # studio until you have watched it; nothing reaches a platform on its own.
    studio_manual_only: bool = True

    # dashboard access (the app is public on Railway unless this is set)
    dashboard_token: str | None = None

    # automation cadence
    scout_enabled: bool = True
    # Which platforms the scout draws from. Comma separated; a platform
    # left out is never searched, whatever keys are configured.
    scout_sources: str = "youtube,reddit"
    scout_interval_minutes: int = 360  # every 6h - hourly is a waste of quota
    scout_keywords: str = ""  # comma separated, per niche
    scout_region: str | None = None
    scout_video_duration: str = "medium"  # short | medium | long
    scout_max_keywords: int = 4
    scout_track_limit: int = 30
    # Quality gate. A 3k-view upload is not a trend, and a clip whose
    # audio is in a language your audience does not speak cannot be
    # captioned into something they will watch.
    scout_min_views: int = 100_000
    scout_language: str = "en"  # blank to accept any language
    # How far back to look. Wider than the view floor suggests, because a
    # six-month-old video with a strong replay peak is better clip material
    # than a fresh one with none - the peak is what gets cut, not the date.
    scout_max_age_days: int = 180

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

    # Running cost estimate shown on the dashboard. Fixed is what you pay
    # whatever happens (host, storage, proxies); per-source is the
    # transcription and model calls one video costs to process.
    cost_fixed_monthly: float = 20.0
    cost_per_source: float = 0.55
    monthly_budget: float = 100.0

    # --- live capture -------------------------------------------------
    #
    # How many streams to hold at once. Ten, and the cost of that is real and
    # worth knowing before the bill arrives rather than after.
    #
    # Each buffer is a continuous 1080p download, because the clips that ship
    # are 1080p and detail that was never downloaded cannot be recovered
    # later. At roughly 8Mbps that is 86GB a day per stream - 860GB a day at
    # ten, against 260GB at three. It is inbound traffic, which Railway does
    # not bill, so this is throughput rather than money: ten streams is a
    # sustained 80Mbps.
    #
    # The CPU is the harder number. Reading the senses on one stream costs
    # about 7 seconds per 30-second window, and every stream is read every 20
    # seconds, so ten streams is around 3.5 cores continuously against 1 for
    # three. Disk is not a problem: the buffer is a rolling five minutes, so
    # ten streams hold about 3GB between them.
    #
    # Ten is here because the question being asked of it is "how many streams
    # does it take to reach ten clips a day", and that is not answerable from
    # three. The Live page reports what each stream actually yields, so the
    # number can come down once the answer is in.
    live_enabled: bool = False
    live_slots: int = 10
    #: Rank a stream must fall past before it is dropped, so two channels
    #: trading places around the cutoff do not cause a reconnect each time.
    #: Held well clear of the slot count: with ten slots the churn around the
    #: cutoff is ten times more likely to happen at all.
    live_drop_rank: int = 16
    live_window_s: float = 300.0
    live_segment_s: float = 4.0
    #: The clip that ships. Buffering below this caps what can ever be posted,
    #: because you cannot recover detail that was never downloaded.
    live_delivery_height: int = 1080
    #: Lead and trail around the moment. Chat reacts after the fact, so most
    #: of the clip is what happened before the reaction.
    live_lead_s: float = 22.0
    live_trail_s: float = 8.0
    #: The longest a clip may run. Short-form platforms stop rewarding much
    #: past this, and a moment that has not ended in a minute is a segment.
    live_max_clip_s: float = 59.0
    #: Below this a window is not a moment, whatever else is true. The caps
    #: cap how many clips a day; this decides whether there is one at all.
    #: Without it the watcher cut its best five minutes of nothing every hour,
    #: because something always scores highest.
    live_min_score: float = 20.0
    #: ...and this much of it has to come from something actually happening
    #: rather than from how busy or loud the channel generally is.
    live_min_event_score: float = 15.0
    #: The last check before a clip exists: a model looks at the frames, the
    #: transcript and the evidence and says whether anything is happening.
    #: Opus because this is the judgement the whole pipeline is built to
    #: reach, and because it is the only thing standing between a bad clip and
    #: an audience once posting is automatic. At about five cents a candidate
    #: and a dozen or so candidates a day it is the cheapest part of the bill;
    #: claude-sonnet-5 is a third of that if it ever stops being.
    verdict_model: str = "claude-opus-5"
    verdict_effort: str = "high"
    #: How many frames it gets to look at. One every three or four seconds.
    verdict_frames: int = 12
    #: Whether it may look at all, and what to do when it cannot. Refusing to
    #: cut something nobody has watched is the right default the moment
    #: posting stops going past a person.
    verdict_enabled: bool = True
    #: A clip nothing watched may not be *posted* automatically. It may still
    #: be kept for a person to look at - those are different questions, and
    #: conflating them cost a day of clips: this used to delete the candidate,
    #: so once the day's look budget was spent nothing could be caught at all.
    verdict_required_to_post: bool = True
    #: How sure it has to be. Refusing a mediocre clip costs one clip; posting
    #: one costs the account.
    verdict_min_confidence: float = 0.55
    #: A ceiling on how many candidates a day may be watched, because a
    #: refused candidate does not count against the clip cap and so cannot
    #: throttle itself. At roughly five cents a look this is the difference
    #: between a predictable bill and an open one. Held in memory, so a deploy
    #: resets it - the bound is a guard against a bad day, not an accountant.
    verdict_per_day: int = 30
    #: How many cut-but-undecided moments to hold while waiting for an output
    #: slot. The buffer only remembers five minutes and the gap between clips
    #: is an hour, so a moment that is not cut immediately is gone - holding
    #: the file is the only way to still have it when the slot opens.
    live_shortlist_max: int = 5
    #: ...and how stale a held moment may be when it is finally used. A good
    #: moment is good whenever it is posted, but not indefinitely.
    live_hold_max_s: float = 2700.0
    #: Who is this streamer, and should the bot watch them at all. One call per
    #: channel with web search, cached for a week, so the bill is a handful of
    #: cents a day however many channels come and go.
    profile_model: str = "claude-opus-5"
    profile_effort: str = "medium"
    #: How sure the research has to be. An unknown streamer is an unknown, not
    #: an approval: being wrong here wastes days of a watch slot.
    profile_min_confidence: float = 0.6
    profile_enabled: bool = True
    #: Whether a channel the research could not reach may be watched anyway.
    #: False means a broken key stops the bot picking anybody new, which is
    #: safer than it quietly going back to watching whatever is biggest.
    profile_required: bool = True
    #: Listening to the words as they are said, rather than only once a clip
    #: has already been chosen. Off by default and metered when on, because
    #: this is the one thing here that costs money per minute of stream rather
    #: than per clip: three streams around the clock is 4,300 stream-minutes a
    #: day, which at any provider's per-minute rate is more than the entire
    #: budget for everything else together. The budget below is in minutes of
    #: audio a day - set it to what you are willing to spend and it will stop
    #: there rather than surprise you.
    speech_live: bool = False
    speech_minutes_per_day: float = 240.0
    #: Only transcribe windows the ear can hear somebody talking in. A stream
    #: playing music to an empty chair is not worth a penny a minute.
    speech_min_share: float = 0.15
    #: How long the words are kept, matching how long chat is kept.
    speech_window_s: float = 300.0
    #: How hard chat has to spike, as a multiple of its own recent baseline.
    live_trigger_ratio: float = 3.0
    #: Nothing is posted anywhere yet - clips are cut and held for review.
    live_posting_enabled: bool = False
    #: Caps. Ten a day is what one page can post without the median dragging
    #: reach down; an hour apart is what stops a burst reading as automated.
    #: How many clips a day are *kept*, not how many are cut. Everything that
    #: clears the bar is cut and ranked; this is where the list is trimmed, and
    #: it trims the weakest rather than the newest.
    live_clips_per_day: int = 60
    #: Zero. There was an hour between clips and it was the single worst rule
    #: in the system: the buffer remembers five minutes, so a moment that was
    #: not cut immediately was gone, and the bot ended up clipping whatever
    #: happened to be happening when the hour turned over. Everything is cut
    #: now and the ranking decides what survives.
    live_min_gap_minutes: int = 0
    #: Directory polling. The listing moves slowly and every call is a request.
    live_roster_poll_s: float = 300.0

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

    # --- surviving an unresolved Railway reference -------------------------
    #
    # A ${{Service.VAR}} reference that cannot resolve does not fail loudly -
    # it becomes an empty string, which reads exactly like "never configured"
    # and sends you to the wrong fix. The managed databases publish the same
    # connection under several names, so rather than depend on one reference
    # being typed correctly, take whichever arrived.
    #
    # Empty is treated as absent throughout, because that is what it means.

    @staticmethod
    def _first(*names: str) -> str | None:
        import os

        for name in names:
            value = (os.environ.get(name) or "").strip()
            if value and not value.startswith("${{"):
                return value
        return None

    @staticmethod
    def _usable(value: str | None) -> bool:
        """A value that is empty, or still a template, is not a URL.

        An unexpanded ${{...}} is worse than an empty one: it is truthy, so it
        sails past every "is it set" check and fails much later as a DNS
        lookup for a hostname with braces in it.
        """
        text = (value or "").strip()
        return bool(text) and not text.startswith("${{")

    @model_validator(mode="after")
    def _recover_connection_urls(self) -> Settings:
        if not self._usable(self.redis_url):
            found = self._first("REDIS_URL", "REDIS_PRIVATE_URL", "REDIS_PUBLIC_URL")
            if found is None:
                found = self._assemble_redis()
            object.__setattr__(self, "redis_url", found)

        if not self._usable(self.database_url):
            object.__setattr__(
                self,
                "database_url",
                self._first("DATABASE_URL", "DATABASE_PRIVATE_URL", "DATABASE_PUBLIC_URL"),
            )
        return self

    @classmethod
    def _assemble_redis(cls) -> str | None:
        """Build a URL from the parts Railway's Redis also publishes."""
        host = cls._first("REDISHOST", "REDIS_HOST")
        if not host:
            return None
        port = cls._first("REDISPORT", "REDIS_PORT") or "6379"
        user = cls._first("REDISUSER", "REDIS_USER") or "default"
        password = cls._first("REDISPASSWORD", "REDIS_PASSWORD")
        credentials = f"{user}:{password}@" if password else ""
        return f"redis://{credentials}{host}:{port}"

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
    def sources(self) -> list[str]:
        return [s.strip().lower() for s in self.scout_sources.split(",") if s.strip()]

    def scouts(self, name: str) -> bool:
        """Whether `name` is a source this deployment draws from."""
        return name.lower() in self.sources

    @property
    def has_reddit(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def reddit_search_terms(self) -> list[str]:
        raw = self.reddit_keywords or self.scout_keywords
        return [k.strip() for k in raw.split(",") if k.strip()]

    @property
    def has_youtube_read(self) -> bool:
        return bool(self.youtube_api_key)

    @property
    def keywords(self) -> list[str]:
        return [k.strip() for k in self.scout_keywords.split(",") if k.strip()]

    @property
    def tts_backend(self) -> str:
        return self.tts_provider.strip().lower()

    @property
    def has_tts(self) -> bool:
        if self.tts_backend == "elevenlabs":
            return bool(self.elevenlabs_api_key)
        return bool(self.openai_api_key)

    @property
    def has_whisper(self) -> bool:
        """Whether the recording can be transcribed with the keys present."""
        return bool(self.openai_api_key or self.transcription_key)

    @property
    def has_stock(self) -> bool:
        return bool(self.pexels_api_key)

    @property
    def studio_ready(self) -> bool:
        """Enough to render something worth watching.

        Narration is the one part with no free fallback: without it a video is
        archive audio and captions, which works but is not the format.
        """
        return self.has_tts

    @property
    def transcription_key(self) -> str | None:
        if self.transcribe_provider == "deepgram":
            return self.deepgram_api_key
        return self.assemblyai_api_key


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
