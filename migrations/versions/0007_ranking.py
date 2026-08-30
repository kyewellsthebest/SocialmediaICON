"""The score that orders the clips, and every number behind it.

Stored rather than computed on read for one reason worth writing down: the
ranking depends on measurements - what was heard, what was seen, whose face
changed - that live nowhere but the clip's own row. Recomputing it later is
possible and that is the point of keeping the parts; recomputing it from
nothing is not.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("catches", sa.Column("rank_score", sa.Float(), nullable=True))
    op.add_column(
        "catches", sa.Column("rank", JSONB(), nullable=False, server_default="{}")
    )
    # What was seen and whose face moved, kept so the ranking can be recomputed
    # when the weights change rather than frozen at whatever they were.
    op.add_column(
        "catches", sa.Column("evidence", JSONB(), nullable=False, server_default="{}")
    )
    # The list is ordered by this on every page load.
    op.create_index("ix_catches_rank_score", "catches", ["rank_score"])


def downgrade() -> None:
    op.drop_index("ix_catches_rank_score", table_name="catches")
    op.drop_column("catches", "evidence")
    op.drop_column("catches", "rank")
    op.drop_column("catches", "rank_score")
