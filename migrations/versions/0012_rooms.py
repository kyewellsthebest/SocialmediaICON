"""Which rooms were read, and what each one gave back.

The room list is the single biggest lever on what this page posts, and until
now there was no way to tell a room that is full of video from one that has
never produced a single usable post - both just contribute to a total. Tuning
it was guesswork, and the list has to be edited in Railway and redeployed to
try anything.

So: results per room on every run, and the list itself editable from the
dashboard. A preference in the database overrides the environment variable, so
the variable stays the default and the way to reset.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_log", sa.Column("rooms", JSONB(), nullable=True))
    op.create_table(
        "preferences",
        sa.Column("name", sa.String(length=64), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("preferences")
    op.drop_column("run_log", "rooms")
