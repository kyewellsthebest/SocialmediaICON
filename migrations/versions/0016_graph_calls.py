"""Count this side of the wire.

Instagram said the publishing limit was spent on a day four things had gone
out. Meta reports its own total and will not itemise it, so there was no way
to tell whether four posts really cost twenty-five - or whether the retries,
each of which creates three containers for a carousel, were the ones spending
it.

Counting here answers that, and gives the daily cap something to refuse
against before a request leaves the building rather than after Meta refuses it.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "graph_calls",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("platform", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ok", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("detail", sa.Text()),
    )
    op.create_index("ix_graph_calls_at", "graph_calls", ["at"])


def downgrade() -> None:
    op.drop_index("ix_graph_calls_at", table_name="graph_calls")
    op.drop_table("graph_calls")
