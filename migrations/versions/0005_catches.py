"""Catches: the text record that outlives the deleted stream.

Revision ID: 0005
Revises: 0004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False, server_default="kick"),
        sa.Column("channel", sa.String(128), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("at_s", sa.Float()),
        sa.Column("duration_s", sa.Float()),
        sa.Column("storage_key", sa.Text()),
        sa.Column("why", JSONB(), nullable=False, server_default="{}"),
        sa.Column("score", sa.Float()),
        sa.Column("mood", JSONB(), nullable=False, server_default="{}"),
        sa.Column("quotes", JSONB(), nullable=False, server_default="[]"),
        sa.Column("peak_viewers", sa.Integer()),
        sa.Column("status", sa.String(32), nullable=False, server_default="caught"),
        sa.Column("source_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    # The two questions actually asked of this table: what did we catch on
    # this channel, and what is the best thing we have not posted yet.
    op.create_index("ix_catches_channel", "catches", ["platform", "channel"])
    op.create_index("ix_catches_status_score", "catches", ["status", "score"])


def downgrade() -> None:
    op.drop_index("ix_catches_status_score", table_name="catches")
    op.drop_index("ix_catches_channel", table_name="catches")
    op.drop_table("catches")
