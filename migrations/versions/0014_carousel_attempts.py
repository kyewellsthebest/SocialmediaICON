"""Remember that the carousel was tried, and how it went.

A failed carousel is left owed deliberately: a storage blip should not cost a
reel its second post permanently. But owed with no memory means retried on
every heartbeat - once a minute, forever - and each attempt wrote its own row,
so one broken render became forty identical red failures with the reason
buried in a tooltip.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reels", sa.Column(
        "carousel_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("reels", sa.Column(
        "carousel_tried_at", sa.DateTime(timezone=True)))

    # The forty rows that already exist say one thing forty times. Keep the
    # most recent attempt per reel and drop the repeats, so the record reads
    # as what happened rather than as how often it was retried.
    op.execute(sa.text("""
        DELETE FROM reel_posts
        WHERE platform = 'instagram_carousel'
          AND id NOT IN (
            SELECT MAX(id) FROM reel_posts
            WHERE platform = 'instagram_carousel'
            GROUP BY reel_id
          )
    """))


def downgrade() -> None:
    op.drop_column("reels", "carousel_tried_at")
    op.drop_column("reels", "carousel_attempts")
