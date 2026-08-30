"""What a model said when it watched the clip, before the clip existed.

Kept for the same reason the rest of the row is kept: this is the record of
*why* a clip exists, and once posting stops going past a person it is the only
account of who approved it. A clip with no verdict is a clip nobody watched,
and that has to be visible rather than inferred.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catches",
        sa.Column("verdict", JSONB(), nullable=False, server_default="{}"),
    )
    # What was actually said, which is half of what the verdict was reading.
    op.add_column("catches", sa.Column("transcript", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("catches", "transcript")
    op.drop_column("catches", "verdict")
