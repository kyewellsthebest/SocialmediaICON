"""credential store so tokens can refresh themselves

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "credentials",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("refreshed_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
    )


def downgrade() -> None:
    op.drop_table("credentials")
