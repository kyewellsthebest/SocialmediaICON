"""The whole schema.

Four tables: the queue, what went out, what each run did, and the tokens.

`reels` is the queue and the archive at once - a row is created the moment a
video is worth keeping and is never deleted, so a video that has been posted
cannot be found and posted again months later. What separates the queue from
the archive is `state`, not the table.

`credentials` survives from the pipeline this replaced because posting has not
changed. There is no table of accounts: INSTAGRAM_USER_ID *is* the Instagram
account, and a handle typed beside it is a second record of the same fact that
can only ever disagree with it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
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

#: JSONB on Postgres, plain JSON everywhere else. Without the variant the
#: tests - which run on SQLite so they can be hermetic - cannot even create
#: the table, so the column type would be the one thing never exercised.
JSON_COLUMN = JSON().with_variant(JSONB(), "postgresql")

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
    #: One row per platform, updated. A row per attempt turns the record of
    #: what happened into a record of how often it was retried.
    __table_args__ = (
        UniqueConstraint("reel_id", "platform", name="uq_reel_post_platform"),
    )

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
    #: When this platform was last asked, successfully or not. Separate from
    #: posted_at, which only ever means "it worked" - a refusal has to be
    #: remembered too, or a retry has nothing to back off from.
    tried_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")

    reel: Mapped[Reel] = relationship(back_populates="posts")


class GraphCall(Base):
    """One call this app made to Meta's publisher, whatever it was for.

    The ledger exists because of a question nothing could answer: Instagram
    said the publishing limit was spent on a day four things had been posted.
    Meta reports its own number and will not itemise it, so the only way to
    know whether four posts really cost twenty-five is to count this side of
    the wire and put the two totals next to each other.

    It is also what the daily cap is enforced from. Meta's limit is a number
    we are told about only after we have exceeded it; this one we can refuse
    against before a single request goes out.
    """

    __tablename__ = "graph_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    #: container - asked Meta to fetch a file and hold it
    #: publish   - turned a container into a post
    #: poll      - asked whether a container had finished
    #: read      - anything else: a permalink, a quota readout, a username
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    detail: Mapped[str | None] = mapped_column(Text)


class RunLog(TimestampMixin, Base):
    """One daily pass, recorded whether it worked or not.

    The failure this exists for: a run happening in a background thread has
    nobody waiting on it and no response to fail, so one that throws is
    completely silent. An empty queue then means either "nothing was found",
    "it never started" or "it crashed", and those are three different things
    to go and fix.
    """

    __tablename__ = "run_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    pushed_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    posted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The traceback, last two thousand characters. A run nobody was watching
    #: leaves nothing else behind.
    error: Mapped[str | None] = mapped_column(Text)

    #: What each room gave back: {room: {read, postable, route, error}}. The
    #: room list is the biggest lever on what gets posted, and a total tells
    #: you nothing about which of twenty-five rooms is carrying it.
    rooms: Mapped[dict[str, Any] | None] = mapped_column(JSON_COLUMN)


class Preference(Base):
    """A setting edited from the dashboard rather than the environment.

    Only the room list so far. It is the biggest lever on what this page
    posts, and needing a Railway redeploy to try a different room is how a
    list stays wrong for a month.

    The environment variable stays the default and the way to reset: an
    absent row means "whatever the variable says".
    """

    __tablename__ = "preferences"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


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
