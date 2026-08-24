"""Postgres schema (spec section 4).

Status/enum-ish columns are stored as plain text with the allowed values kept
next to them as module constants — the set of platforms and statuses changes
faster than it is worth writing migrations for native enums.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# --- allowed values -------------------------------------------------------

LICENSES = ("own", "licensed", "campaign", "permitted", "none")
SOURCE_KINDS = ("youtube", "podcast", "upload")
SOURCE_STATUSES = (
    "registered",
    "downloading",
    "downloaded",
    "transcribing",
    "transcribed",
    "detecting",
    "ranking",
    "rendering",
    "done",
    "failed",
)
CANDIDATE_STATUSES = ("new", "ranked", "selected", "rejected", "rendered")
CLIP_STATUSES = ("rendered", "queued", "approved", "posted", "rejected")
PLATFORMS = ("youtube", "instagram", "tiktok", "facebook", "snapchat")
JOB_STATES = ("queued", "running", "done", "failed")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Niche(TimestampMixin, Base):
    __tablename__ = "niches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # config: {"cadence": "2/day", "caption_style": "karaoke_bold", ...}
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    accounts: Mapped[list[Account]] = relationship(back_populates="niche")
    sources: Mapped[list[Source]] = relationship(back_populates="niche")


class Account(TimestampMixin, Base):
    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    handle: Mapped[str] = mapped_column(String(120), nullable=False)
    # auth_ref points at the secret store / env key holding the token, never the token itself
    auth_ref: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")

    niche: Mapped[Niche | None] = relationship(back_populates="accounts")


class Source(TimestampMixin, Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id", ondelete="SET NULL"))
    url: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube")
    # The whole legal posture of the project hangs off this column.
    license: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    duration_s: Mapped[float | None] = mapped_column(Float)
    storage_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="registered")
    error: Mapped[str | None] = mapped_column(Text)

    niche: Mapped[Niche | None] = relationship(back_populates="sources")
    transcript: Mapped[Transcript | None] = relationship(
        back_populates="source", uselist=False, cascade="all, delete-orphan"
    )
    candidates: Mapped[list[Candidate]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Transcript(TimestampMixin, Base):
    __tablename__ = "transcripts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    # words: [{"w": "hello", "start": 12.34, "end": 12.55}, ...]
    words: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)
    full_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    provider: Mapped[str] = mapped_column(String(32), nullable=False)

    source: Mapped[Source] = relationship(back_populates="transcript")


class Candidate(TimestampMixin, Base):
    __tablename__ = "candidates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(
        ForeignKey("sources.id", ondelete="CASCADE"), nullable=False
    )
    start_s: Mapped[float] = mapped_column(Float, nullable=False)
    end_s: Mapped[float] = mapped_column(Float, nullable=False)
    hook_score: Mapped[float | None] = mapped_column(Float)
    emotion: Mapped[str | None] = mapped_column(String(32))
    payoff_score: Mapped[float | None] = mapped_column(Float)
    context_ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    novelty: Mapped[float | None] = mapped_column(Float)
    predicted_score: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")

    source: Mapped[Source] = relationship(back_populates="candidates")
    clips: Mapped[list[Clip]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class Clip(TimestampMixin, Base):
    __tablename__ = "clips"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    candidate_id: Mapped[int] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    storage_key: Mapped[str | None] = mapped_column(Text)
    caption_style: Mapped[str] = mapped_column(String(64), nullable=False, default="karaoke")
    title: Mapped[str | None] = mapped_column(Text)
    hashtags: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    duration_s: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="rendered")

    candidate: Mapped[Candidate] = relationship(back_populates="clips")
    posts: Mapped[list[Post]] = relationship(back_populates="clip", cascade="all, delete-orphan")


class Post(TimestampMixin, Base):
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clip_id: Mapped[int] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_post_id: Mapped[str | None] = mapped_column(String(200))
    platform_url: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    clip: Mapped[Clip] = relationship(back_populates="posts")
    snapshots: Mapped[list[MetricSnapshot]] = relationship(
        back_populates="post", cascade="all, delete-orphan"
    )


class MetricSnapshot(Base):
    """One row per pull — this is the time series Phase 4 learns from."""

    __tablename__ = "metric_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    views: Mapped[int | None] = mapped_column(Integer)
    likes: Mapped[int | None] = mapped_column(Integer)
    comments: Mapped[int | None] = mapped_column(Integer)
    shares: Mapped[int | None] = mapped_column(Integer)
    saves: Mapped[int | None] = mapped_column(Integer)
    avg_watch_s: Mapped[float | None] = mapped_column(Float)
    completion_rate: Mapped[float | None] = mapped_column(Float)

    post: Mapped[Post] = relationship(back_populates="snapshots")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    error: Mapped[str | None] = mapped_column(Text)


# --- Phase 4: trend scouting -------------------------------------------------

TRACKED_STATUSES = ("new", "queued", "clipped", "ignored")


class TrackedVideo(TimestampMixin, Base):
    """A public video we are watching because it is performing well.

    One row per source video per platform. The performance numbers here are the
    latest reading; the history lives in `tracked_snapshots`.
    """

    __tablename__ = "tracked_videos"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_tracked_platform_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    niche_id: Mapped[int | None] = mapped_column(ForeignKey("niches.id", ondelete="SET NULL"))
    platform: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube")
    external_id: Mapped[str] = mapped_column(String(120), nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str | None] = mapped_column(Text)
    channel_id: Mapped[str | None] = mapped_column(String(120))
    channel_title: Mapped[str | None] = mapped_column(Text)
    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_s: Mapped[float | None] = mapped_column(Float)

    views: Mapped[int | None] = mapped_column(BigInteger)
    likes: Mapped[int | None] = mapped_column(BigInteger)
    comments: Mapped[int | None] = mapped_column(BigInteger)

    # views per hour, measured between the last two snapshots where possible
    velocity_vph: Mapped[float | None] = mapped_column(Float)
    like_rate: Mapped[float | None] = mapped_column(Float)
    # composite 0-100 used to order the trending table
    score: Mapped[float | None] = mapped_column(Float)

    # raw most-replayed curve from yt-dlp: [{"start","end","value"}]
    heatmap: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # peaks worth clipping: [{"start_s","end_s","value"}]
    hot_segments: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)

    status: Mapped[str] = mapped_column(String(32), nullable=False, default="new")
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    snapshots: Mapped[list[TrackedSnapshot]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class TrackedSnapshot(Base):
    """One reading of a tracked video's counters. Append only."""

    __tablename__ = "tracked_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tracked_video_id: Mapped[int] = mapped_column(
        ForeignKey("tracked_videos.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    views: Mapped[int | None] = mapped_column(BigInteger)
    likes: Mapped[int | None] = mapped_column(BigInteger)
    comments: Mapped[int | None] = mapped_column(BigInteger)

    video: Mapped[TrackedVideo] = relationship(back_populates="snapshots")


class Credential(Base):
    """A token the process refreshes for itself.

    Meta's tokens last 60 days and can be extended indefinitely, but only by
    calling an endpoint before they lapse - and a process cannot rewrite its own
    environment. So the current value lives here instead: seeded from the
    environment on first use, then replaced by the refresh job. The environment
    variable stays the fallback and the way you rotate a token by hand.
    """

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The environment variable this shadows, e.g. THREADS_ACCESS_TOKEN.
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class ApiQuota(Base):
    """Daily API spend, so a scout run can refuse to blow the free tier."""

    __tablename__ = "api_quota"
    __table_args__ = (UniqueConstraint("day", "service", name="uq_quota_day_service"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    service: Mapped[str] = mapped_column(String(32), nullable=False)
    units: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
