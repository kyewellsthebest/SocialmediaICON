"""How each clip was framed, kept with the clip.

A desk stream is stacked - webcam over the middle of the screen - and
everything else follows the action. Which one happened is the first thing
anyone asks when a clip looks wrong, and it cannot be worked out afterwards:
the decision is made from a face detection on a file that is deleted once the
portrait version exists.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "catches", sa.Column("framing", JSONB(), nullable=False, server_default="{}")
    )


def downgrade() -> None:
    op.drop_column("catches", "framing")
