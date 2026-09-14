"""Environment configuration.

Everything is optional, so the app boots and tells you what is missing rather
than refusing to start with a stack trace. Each subsystem exposes a property
that says whether it is actually wired up, and the callers check it.

What is *not* here is as deliberate as what is. There is no model key, no
transcription key, no stock-footage key and no speech key, because nothing in
this system reads, watches or writes anything. A finished video is downloaded,
a badge is drawn on it, and the author's own caption goes out with it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


def _names(raw: str) -> list[str]:
    """A comma-separated setting, as a list of names.

    Forgiving on purpose, because the failure it prevents is total and silent.
    A dashboard variable once arrived in production as "=irl" - the name went
    in the name box and "=irl" in the value box - so the filter looked for a
    thing called "=irl", matched nothing, and every page said it was fine.

    So an entry is stripped of spaces and quotes, and anything up to and
    including a "=" is dropped: "=GYM", "GYM", ' "gym" ' and a whole pasted
    REDDIT_SUBREDDITS=GYM all mean GYM. A name cannot contain an equals sign,
    so nothing legitimate is lost.
    """
    # Commas, newlines and semicolons all separate. The dashboard's room
    # editor is one per line and a pasted list is usually comma-separated;
    # splitting on only one of those turns the whole box into a single room
    # name, which then reads as "every room is dead".
    out: list[str] = []
    for part in re.split(r"[,\n;]", raw or ""):
        name = part.strip().strip("\"'").strip()
        if "=" in name:
            name = name.rsplit("=", 1)[1].strip().strip("\"'").strip()
        name = name.removeprefix("r/").removeprefix("/r/").strip()
        if name:
            out.append(name)
    return out


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    env: str = "dev"

    # --- infrastructure ---------------------------------------------------
    database_url: str | None = None

    r2_account_id: str | None = None
    r2_access_key_id: str | None = None
    r2_secret_access_key: str | None = None
    r2_bucket: str | None = None

    #: The dashboard is reachable from the public internet the moment it
    #: deploys. Without this, so is the button that posts things.
    dashboard_token: str | None = None

    work_dir: Path = REPO_ROOT / ".work"
    local_storage_dir: Path = REPO_ROOT / ".storage"

    # --- where the video comes from ---------------------------------------
    #: A free script app at reddit.com/prefs/apps. Not required - the feed
    #: routes need no app at all - but it is the only route that can search,
    #: and it reports vote counts even when a subreddit's feed does not.
    reddit_client_id: str | None = None
    reddit_client_secret: str | None = None
    reddit_user_agent: str = "putitupp/1.0 (repost queue)"

    #: The default rooms. The dashboard's list overrides this when one is
    #: saved; an empty list there hands control back here.
    #:
    #: Not optional: blank would mean site-wide search, and search exists only
    #: on the JSON routes - the ones a cloud host is refused from.
    #:
    #: Chosen for PR attempts, heavy singles, skills and fails - the things
    #: people post *as video*. Progress, motivation and physique rooms are
    #: deliberately absent: they are mostly photographs and text, and the few
    #: videos are personal rather than watchable by a stranger.
    #:
    #: Some of these names may not exist or may be dead. That is expected and
    #: visible: a room that cannot be read reports the error against itself in
    #: the Setup tab's per-room table, and one that is read but never yields
    #: anything shows as a row of zeros. Prune it there after a week rather
    #: than guessing here.
    reddit_subreddits: str = (
        # Lifting, where a PR attempt is the point of the post.
        "GYM,powerlifting,weightlifting,Olympicweightlifting,strongman,"
        "weightroom,powerbuilding,bodybuilding,naturalbodybuilding,"
        # Skills, which are visually strong and already vertical.
        "calisthenics,bodyweightfitness,Gymnastics,kettlebell,crossfit,"
        # Fails.
        "gymfails,Weightliftingfail,fitnessfails,"
        # Wide enough to be worth reading, gym enough to stay on topic.
        "fitness,Fitnesscirclejerk,GYMBROS"
    )
    #: hour | day | week | month | year | all. The window the rooms are ranked
    #: over. Wider finds better video and finds the same video every day;
    #: narrower keeps the queue fresh and can come up empty on a quiet room.
    reddit_time_filter: str = "week"
    #: Sixty seconds is where Reels, Shorts and TikTok all stop treating a
    #: video as short-form. Two seconds is not a post, hence the floor.
    reddit_max_duration_s: float = 60.0
    reddit_floor_duration_s: float = 4.0
    #: Only applied on routes that can see a score. The feed routes cannot, and
    #: their top-of-window sort is standing in for it.
    reddit_min_upvotes: int = 500
    #: How many entries to read per room per run. The feed routes look each
    #: candidate up individually, so this multiplied by the room count is the
    #: length of a run.
    reddit_per_room: int = 12

    #: Which ways in to try, in order. Blank tries all six. Pin it once you
    #: know which one this host can use - the others cost a failed request
    #: each before they give up. oauth, json, proxied, reader, mirror, rss
    reddit_routes: str = ""
    reddit_proxy: str | None = None
    #: A public reader service, which fetches a page and returns its text.
    #: Their address does the asking, which is the point.
    reddit_reader: str = "https://r.jina.ai"
    reddit_reader_key: str | None = None
    #: Redlib front-ends: a different domain, so a block on reddit.com does
    #: not reach them. Public instances come and go, hence configuration.
    reddit_mirrors: str = (
        "https://safereddit.com,"
        "https://redlib.catsarch.com,"
        "https://redlib.perennialte.ch,"
        "https://l.opnxng.com,"
        "https://redlib.privacyredirect.com"
    )

    # --- yt-dlp -----------------------------------------------------------
    #: Only needed if Reddit starts refusing this host's address. Accepts full
    #: URLs or the ip:port:user:pass lines proxy dashboards export.
    ytdlp_proxy: str | None = None
    ytdlp_proxies: str | None = None
    ytdlp_max_proxies_per_run: int = 4

    # --- branding ---------------------------------------------------------
    #: The badge's width as a share of the video's own width. A fraction
    #: rather than pixels: the same 96px logo is a discreet mark on a
    #: 1080-wide video and a sticker over someone's face on a 480-wide one.
    brand_width_share: float = 0.13
    #: How far down from the top edge, as a share of the width so it scales
    #: with the badge. Small: the mark sits just under the top edge, centred,
    #: clear of the corners where every platform draws its own furniture.
    brand_inset_share: float = 0.03
    brand_opacity: float = 1.0
    #: 18 is visually lossless for this kind of source; the file is small
    #: because the video is short, not because it is squeezed.
    brand_crf: int = 18

    # --- the queue --------------------------------------------------------
    harvest_enabled: bool = True
    #: Daily. Top-of-window is a settled list, so a second pass at it mostly
    #: re-reads posts the queue already knows about.
    harvest_interval_minutes: int = 24 * 60
    #: How many wait their turn. A run that finds more than this keeps the
    #: best, and a later run with a better video pushes the weakest out.
    queue_size: int = 15
    # --- the Instagram carousel --------------------------------------------
    #: A second post of the same video: a square cover, then the square
    #: video. A reel goes to the Reels surface and a carousel sits in the grid
    #: and the feed, so the same clip reaches two different sets of eyes
    #: without either looking like a repeat.
    carousel_enabled: bool = True
    #: How long after the reel. Far enough apart not to read as a double
    #: post, close enough to ride the same interest.
    carousel_delay_minutes: int = 30
    #: Where the cover still comes from. Not zero: the literal first frame is
    #: very often black, a fade, or a hand reaching for the phone.
    carousel_frame_at_s: float = 1.0
    carousel_swipe_text: str = "Swipe to watch"
    #: How many goes before giving up. A render that fails on a bad frame
    #: fails the same way every time, and retrying it once a minute for a week
    #: buries every other failure on the page.
    carousel_max_attempts: int = 4
    #: Multiplied by the attempt number, so the gaps widen: 15, 30, 45...
    carousel_retry_minutes: int = 15
    #: How long to stand down when Meta says the publishing limit is spent.
    #: Its window is 24 hours and the allowance refills gradually, so an hour
    #: is a reasonable next look - and asking more often is what spends it.
    rate_limit_wait_minutes: int = 60
    #: Ask again on a platform that answered "not now". A reel counts as
    #: posted the moment any platform takes it, so without this a reel that
    #: went to Facebook and hit Instagram's limit is finished with Instagram
    #: still owed - and the limit clears in an hour.
    retry_rate_limited: bool = True
    #: How long a refused platform stays worth retrying. Past a day the clip
    #: is no longer the clip of the day, and the queue has moved on.
    retry_within_hours: int = 24

    # --- when reels go out -------------------------------------------------
    #: The clock the schedule is read against. Railway runs in UTC, so "7am"
    #: means seven in the morning *somewhere*, and the somewhere has to be
    #: said out loud: posting at 07:00 UTC to an audience in Brisbane puts
    #: every reel out at five in the afternoon.
    post_timezone: str = "Australia/Brisbane"
    #: The first slot of the day, local.
    post_start_hour: int = 7
    #: How many go out in a day, one per slot.
    post_per_day: int = 5
    #: How far apart the slots are. 5 an hour apart from 07:00 means the last
    #: lands at 11:00.
    post_every_minutes: int = 60

    # --- publishing -------------------------------------------------------
    publisher: str = "manual"  # manual | upload_post | youtube | meta
    #: Nothing is posted until this is on, whatever else is configured.
    autopost_enabled: bool = False

    upload_post_api_key: str | None = None
    upload_post_user: str | None = None
    upload_post_base_url: str = "https://api.upload-post.com"
    #: Which platforms the reseller should post to. Unlike the Meta ones this
    #: is a decision rather than a credential - the same key reaches all of
    #: them - so it has to be named rather than derived.
    upload_post_platforms: str = "tiktok,snapchat"

    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_refresh_token: str | None = None

    meta_graph_version: str = "v21.0"
    meta_app_id: str | None = None
    meta_app_secret: str | None = None
    meta_access_token: str | None = None
    instagram_user_id: str | None = None
    instagram_access_token: str | None = None
    instagram_app_id: str | None = None
    instagram_app_secret: str | None = None
    facebook_page_id: str | None = None
    facebook_page_token: str | None = None
    threads_user_id: str | None = None
    threads_access_token: str | None = None
    threads_app_id: str | None = None
    threads_app_secret: str | None = None
    meta_publish_timeout_s: int = 300
    #: Meta tokens die at 60 days and cannot be revived afterwards, so the
    #: refresh runs at a quarter of that: three failed runs still leave a
    #: fortnight of slack.
    token_refresh_interval_days: int = 14

    # --- derived ----------------------------------------------------------

    @property
    def is_prod(self) -> bool:
        return self.env.lower() in ("prod", "production")

    @property
    def has_db(self) -> bool:
        return bool(self.database_url)

    @property
    def has_storage(self) -> bool:
        return all(
            [self.r2_account_id, self.r2_access_key_id,
             self.r2_secret_access_key, self.r2_bucket]
        )

    #: The publishers spell it this way, because R2 is the specific thing
    #: Meta needs: it downloads the file from a URL rather than accepting an
    #: upload, so a local disk is not a substitute.
    @property
    def has_r2(self) -> bool:
        return self.has_storage

    @property
    def has_reddit(self) -> bool:
        return bool(self.reddit_client_id and self.reddit_client_secret)

    @property
    def reddit_rooms(self) -> list[str]:
        return _names(self.reddit_subreddits)

    @property
    def reddit_route_names(self) -> list[str]:
        return [r.lower() for r in _names(self.reddit_routes)]

    @property
    def reddit_mirror_list(self) -> list[str]:
        return [m.rstrip("/") for m in (self.reddit_mirrors or "").split(",") if m.strip()]

    @property
    def sqlalchemy_url(self) -> str:
        """DATABASE_URL, spelled the way SQLAlchemy wants it.

        Two rewrites, and both are needed. Railway and Heroku hand out
        postgres://, which SQLAlchemy 2 does not answer to at all; and plain
        postgresql:// makes it reach for psycopg2, which is not what is
        installed. Neither failure names the URL as the problem.
        """
        if not self.database_url:
            raise RuntimeError("DATABASE_URL is not set")
        url = self.database_url
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        if url.startswith("postgresql://"):
            # ...and onto psycopg 3, which is what is installed. Without this
            # SQLAlchemy reaches for psycopg2, is not given it, and reports
            # "No module named 'psycopg2'" - which reads as a missing
            # dependency rather than as a URL that names the wrong driver.
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        return url

    @property
    def r2_endpoint_url(self) -> str:
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @property
    def instagram_via_instagram_login(self) -> bool:
        """True when Instagram is reached directly rather than through a Page."""
        return bool(self.instagram_access_token)

    @property
    def has_instagram(self) -> bool:
        return bool(
            self.instagram_user_id
            and (self.instagram_access_token or self.meta_access_token)
        )

    @property
    def has_threads(self) -> bool:
        return bool(self.threads_user_id and self.threads_access_token)

    @property
    def has_facebook(self) -> bool:
        return bool(self.facebook_page_id and (self.facebook_page_token or self.meta_access_token))

    @property
    def has_meta_tokens(self) -> bool:
        return bool(
            self.meta_access_token
            or self.instagram_access_token
            or self.threads_access_token
            or self.facebook_page_token
        )

    @property
    def has_upload_post(self) -> bool:
        return bool(self.upload_post_api_key and self.upload_post_user)

    @property
    def has_youtube_write(self) -> bool:
        return bool(
            self.youtube_client_id and self.youtube_client_secret and self.youtube_refresh_token
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
