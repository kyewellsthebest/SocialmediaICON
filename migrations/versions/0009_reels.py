"""One job, four tables.

The clipping pipeline is gone - no sources, no transcripts, no candidates, no
clips, no trend scouting - and with it the eleven tables that existed to carry
a video through being cut up. What replaces them is a video that arrives
finished: a queue of the fifteen best, and a record of what was posted.

`accounts` and `credentials` stay. Posting did not change.

This removes the old tables. Everything in them belonged to a pipeline that no
longer has code to read it, so there is nothing to carry across - and leaving
unreadable tables behind is how a schema stops being documentation.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Children first: every one of these is pointed at by something below it.
RETIRED = (
    "metric_snapshots",
    "posts",
    "clips",
    "candidates",
    "transcripts",
    "sources",
    "tracked_snapshots",
    "tracked_videos",
    "catches",
    "renders",
    "jobs",
    "api_quota",
    "niches",
)


def upgrade() -> None:
    op.create_table(
        "reels",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("external_id", sa.String(length=20), nullable=False),
        sa.Column("permalink", sa.Text(), nullable=False),
        sa.Column("subreddit", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("author", sa.String(length=80)),
        sa.Column("caption", sa.Text(), nullable=False, server_default=""),
        sa.Column("ups", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_s", sa.Float()),
        sa.Column("posted_to_reddit_at", sa.DateTime(timezone=True)),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="found"),
        sa.Column("note", sa.Text()),
        sa.Column("storage_key", sa.Text()),
        sa.Column("local_path", sa.Text()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("external_id", name="uq_reel_external_id"),
    )
    # The queue reads "the best fifteen not yet posted" on every run, and the
    # dedupe reads "have we seen this id" once per candidate found.
    op.create_index("ix_reels_state_ups", "reels", ["state", "ups"])

    op.create_table(
        "reel_posts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("reel_id", sa.Integer(),
                  sa.ForeignKey("reels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("platform_post_id", sa.String(length=200)),
        sa.Column("platform_url", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.create_index("ix_reel_posts_reel", "reel_posts", ["reel_id"])

    # accounts.niche_id pointed at a table that is going away. The foreign
    # key's name is asked for rather than assumed: Alembic did not name it,
    # Postgres did, and a guess that is wrong fails the whole deploy on a
    # line that has nothing to do with the change being made.
    inspector = sa.inspect(op.get_bind())
    columns = {c["name"] for c in inspector.get_columns("accounts")}
    if "niche_id" in columns:
        for key in inspector.get_foreign_keys("accounts"):
            if "niche_id" in key.get("constrained_columns", []) and key.get("name"):
                op.drop_constraint(key["name"], "accounts", type_="foreignkey")
        op.drop_column("accounts", "niche_id")

    for table in RETIRED:
        op.execute(sa.text(f'DROP TABLE IF EXISTS "{table}" CASCADE'))


def downgrade() -> None:
    # One way. The retired tables belonged to code that no longer exists, so
    # recreating them empty would restore the shape and none of the meaning.
    op.drop_index("ix_reel_posts_reel", table_name="reel_posts")
    op.drop_table("reel_posts")
    op.drop_index("ix_reels_state_ups", table_name="reels")
    op.drop_table("reels")
