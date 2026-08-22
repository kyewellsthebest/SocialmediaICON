"""trend scouting tables + post link/error columns

Revision ID: 0002
Revises: 0001
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("posts", sa.Column("platform_url", sa.Text()))
    op.add_column("posts", sa.Column("error", sa.Text()))

    op.create_table(
        "tracked_videos",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("niche_id", sa.Integer, sa.ForeignKey("niches.id", ondelete="SET NULL")),
        sa.Column("platform", sa.String(32), nullable=False, server_default="youtube"),
        sa.Column("external_id", sa.String(120), nullable=False),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("channel_id", sa.String(120)),
        sa.Column("channel_title", sa.Text),
        sa.Column("thumbnail_url", sa.Text),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("duration_s", sa.Float),
        sa.Column("views", sa.BigInteger),
        sa.Column("likes", sa.BigInteger),
        sa.Column("comments", sa.BigInteger),
        sa.Column("velocity_vph", sa.Float),
        sa.Column("like_rate", sa.Float),
        sa.Column("score", sa.Float),
        sa.Column("heatmap", JSONB),
        sa.Column("hot_segments", JSONB),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("platform", "external_id", name="uq_tracked_platform_id"),
    )
    op.create_index("ix_tracked_videos_score", "tracked_videos", ["score"])
    op.create_index("ix_tracked_videos_status", "tracked_videos", ["status"])

    op.create_table(
        "tracked_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "tracked_video_id",
            sa.Integer,
            sa.ForeignKey("tracked_videos.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("views", sa.BigInteger),
        sa.Column("likes", sa.BigInteger),
        sa.Column("comments", sa.BigInteger),
    )
    op.create_index(
        "ix_tracked_snapshots_video", "tracked_snapshots", ["tracked_video_id", "captured_at"]
    )

    op.create_table(
        "api_quota",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("service", sa.String(32), nullable=False),
        sa.Column("units", sa.Integer, nullable=False, server_default="0"),
        sa.UniqueConstraint("day", "service", name="uq_quota_day_service"),
    )


def downgrade() -> None:
    op.drop_table("api_quota")
    op.drop_index("ix_tracked_snapshots_video", table_name="tracked_snapshots")
    op.drop_table("tracked_snapshots")
    op.drop_index("ix_tracked_videos_status", table_name="tracked_videos")
    op.drop_index("ix_tracked_videos_score", table_name="tracked_videos")
    op.drop_table("tracked_videos")
    op.drop_column("posts", "error")
    op.drop_column("posts", "platform_url")
