"""The second post: the same video as a square Instagram carousel.

A reel goes to the Reels surface; a carousel sits in the grid and the feed.
The same clip posted as both, half an hour apart, reaches two different sets
of eyes without either looking like a repeat - so the carousel needs its own
timestamp rather than sharing the reel's.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reels", sa.Column("carousel_at", sa.DateTime(timezone=True)))
    op.add_column("reels", sa.Column("carousel_note", sa.Text()))
    # Every read is "posted, carousel still owed" - a tiny slice of a table
    # that only grows.
    op.create_index("ix_reels_carousel_owed", "reels", ["state", "carousel_at"])


def downgrade() -> None:
    op.drop_index("ix_reels_carousel_owed", table_name="reels")
    op.drop_column("reels", "carousel_note")
    op.drop_column("reels", "carousel_at")
