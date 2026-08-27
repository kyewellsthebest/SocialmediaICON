"""Studio renders: original videos made from public-record audio.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "renders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("archive_id", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("duration_s", sa.Float(), nullable=True),
        sa.Column("options", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("layers", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("warnings", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("cost_usd", sa.Float(), nullable=True),
        sa.Column("elapsed_s", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # The studio list is always "newest first, optionally one status".
    op.create_index("ix_renders_status_id", "renders", ["status", "id"])


def downgrade() -> None:
    op.drop_index("ix_renders_status_id", table_name="renders")
    op.drop_table("renders")
