"""initial schema (spec section 4)

Revision ID: 0001
Revises:
Create Date: 2026-08-21
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "niches",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False, unique=True),
        sa.Column("config", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("niche_id", sa.Integer, sa.ForeignKey("niches.id", ondelete="SET NULL")),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("handle", sa.String(120), nullable=False),
        sa.Column("auth_ref", sa.String(200)),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "sources",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("niche_id", sa.Integer, sa.ForeignKey("niches.id", ondelete="SET NULL")),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="youtube"),
        sa.Column("license", sa.String(32), nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("duration_s", sa.Float),
        sa.Column("storage_key", sa.Text),
        sa.Column("status", sa.String(32), nullable=False, server_default="registered"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_sources_status", "sources", ["status"])

    op.create_table(
        "transcripts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer,
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("words", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("full_text", sa.Text, nullable=False, server_default=""),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "candidates",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source_id", sa.Integer, sa.ForeignKey("sources.id", ondelete="CASCADE"), nullable=False),
        sa.Column("start_s", sa.Float, nullable=False),
        sa.Column("end_s", sa.Float, nullable=False),
        sa.Column("hook_score", sa.Float),
        sa.Column("emotion", sa.String(32)),
        sa.Column("payoff_score", sa.Float),
        sa.Column("context_ok", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("novelty", sa.Float),
        sa.Column("predicted_score", sa.Float),
        sa.Column("rationale", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(32), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_candidates_source_id", "candidates", ["source_id"])

    op.create_table(
        "clips",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("candidate_id", sa.Integer, sa.ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False),
        sa.Column("storage_key", sa.Text),
        sa.Column("caption_style", sa.String(64), nullable=False, server_default="karaoke"),
        sa.Column("title", sa.Text),
        sa.Column("hashtags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("duration_s", sa.Float),
        sa.Column("status", sa.String(32), nullable=False, server_default="rendered"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_clips_status", "clips", ["status"])

    op.create_table(
        "posts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("clip_id", sa.Integer, sa.ForeignKey("clips.id", ondelete="CASCADE"), nullable=False),
        sa.Column("account_id", sa.Integer, sa.ForeignKey("accounts.id", ondelete="SET NULL")),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("platform_post_id", sa.String(200)),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "metric_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("post_id", sa.Integer, sa.ForeignKey("posts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("views", sa.Integer),
        sa.Column("likes", sa.Integer),
        sa.Column("comments", sa.Integer),
        sa.Column("shares", sa.Integer),
        sa.Column("saves", sa.Integer),
        sa.Column("avg_watch_s", sa.Float),
        sa.Column("completion_rate", sa.Float),
    )
    op.create_index("ix_metric_snapshots_post_id", "metric_snapshots", ["post_id", "captured_at"])

    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("state", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("error", sa.Text),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("jobs")
    op.drop_index("ix_metric_snapshots_post_id", table_name="metric_snapshots")
    op.drop_table("metric_snapshots")
    op.drop_table("posts")
    op.drop_index("ix_clips_status", table_name="clips")
    op.drop_table("clips")
    op.drop_index("ix_candidates_source_id", table_name="candidates")
    op.drop_table("candidates")
    op.drop_table("transcripts")
    op.drop_index("ix_sources_status", table_name="sources")
    op.drop_table("sources")
    op.drop_table("accounts")
    op.drop_table("niches")
