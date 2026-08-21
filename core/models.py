"""Postgres schema (spec section 4).

Status/enum-ish columns are stored as plain text with the allowed values kept
next to them as module constants — the set of platforms and statuses changes
faster than it is worth writing migrations for native enums.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
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
