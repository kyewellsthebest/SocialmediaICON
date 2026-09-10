"""The whole schema.

Four tables, because the job is four things: find a video, keep the best
fifteen, brand one, post it.

`reels` is the queue and the archive at once - a row is created the moment a
video is worth keeping and is never deleted, so a video that has been posted
cannot be found and posted again months later. What separates the queue from
the archive is `state`, not the table.

`accounts` and `credentials` survive from the pipeline this replaced because
posting has not changed: the same four publishers need to know which handles
to post to and which tokens to use.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

PLATFORMS = ("youtube", "instagram", "tiktok", "facebook", "snapchat", "threads")

#: A reel's life, in order.
#:
#: found     in the queue, competing on upvotes for one of the fifteen slots
#: ready     downloaded and branded, waiting its turn to be posted
#: posted    out. Kept forever so it is never picked up a second time
#: dropped   pushed out of the queue by better videos, or refused on the way
#:           through. Kept for the same reason.
REEL_STATES = ("found", "ready", "posted", "dropped")


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Reel(TimestampMixin, Base):
    """One Reddit video, from the moment it is worth keeping to after it posts."""

    __tablename__ = "reels"
    __table_args__ = (UniqueConstraint("external_id", name="uq_reel_external_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # --- who made it, and where it came from. This is the attribution, and
    # it is the answer to "which post was this?" when somebody writes in
    # asking for their video to be taken down.
    external_id: Mapped[str] = mapped_column(String(20), nullable=False)
    permalink: Mapped[str] = mapped_column(Text, nullable=False)
    subreddit: Mapped[str] = mapped_column(String(80), nullable=False, default="")
    author: Mapped[str | None] = mapped_column(String(80))

    #: The author's own words about their own video, verbatim. This is what
    #: gets posted as the caption - not a rewrite of it.
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")

    #: What the queue ranks on. Everything else about a video is a matter of
    #: taste; this is the one number several thousand people already voted on.
    ups: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duration_s: Mapped[float | None] = mapped_column(Float)
    posted_to_reddit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    state: Mapped[str] = mapped_column(String(16), nullable=False, default="found")
    #: Why it was dropped, when it was. A queue that silently discards things
    #: is a queue nobody can debug.
    note: Mapped[str | None] = mapped_column(Text)

    #: Where the branded file lives. Local path on the worker, or an object
    #: key once storage is configured.
    storage_key: Mapped[str | None] = mapped_column(Text)
    local_path: Mapped[str | None] = mapped_column(Text)

    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    posts: Mapped[list[ReelPost]] = relationship(
        back_populates="reel", cascade="all, delete-orphan"
    )


class ReelPost(TimestampMixin, Base):
    """One attempt to put one reel on one platform."""

    __tablename__ = "reel_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    reel_id: Mapped[int] = mapped_column(
        ForeignKey("reels.id", ondelete="CASCADE"), nullable=False
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    platform_post_id: Mapped[str | None] = mapped_column(String(200))
    platform_url: Mapped[str | None] = mapped_column(Text)
    #: Kept rather than raised: one platform refusing is not the others
    #: refusing, and a failure nobody recorded is a failure nobody fixes.
    error: Mapped[str | None] = mapped_column(Text)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    reel: Mapped[Reel] = relationship(back_populates="posts")


class Account(TimestampMixin, Base):
    """A handle to post to."""

    __tablename__ = "accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    handle: Mapped[str] = mapped_column(String(120), nullable=False)
    # Points at the secret store / env key holding the token, never the token.
    auth_ref: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")


class Credential(Base):
    """A token the process refreshes for itself.

    Meta's tokens last 60 days and can be extended indefinitely, but only by
    calling an endpoint before they lapse - and a process cannot rewrite its
    own environment. So the current value lives here instead: seeded from the
    environment on first use, then replaced by the refresh job. The environment
    variable stays the fallback and the way you rotate a token by hand.
    """

    __tablename__ = "credentials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
