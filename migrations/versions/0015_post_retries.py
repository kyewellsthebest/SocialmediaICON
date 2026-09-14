"""Remember when each platform was last tried, so a refusal can be re-tried.

Instagram's publishing limit is 25 posts per account per rolling 24 hours, and
it is spent by asking, not only by succeeding. When it ran out, every reel's
Instagram attempt failed - and because Facebook succeeded on the same reel, the
reel was marked posted and Instagram was never asked again. A limit that clears
by itself in an hour cost a day of Instagram posts permanently.

Retrying needs two things this table did not have: one row per platform rather
than one per attempt, and a record of when that attempt was made.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("reel_posts", sa.Column("tried_at", sa.DateTime(timezone=True)))

    # Backfill from what the row already knows, so existing rows are not all
    # "never tried" and eligible for an immediate retry the moment this
    # deploys - which is the traffic spike that caused the problem.
    op.execute(sa.text("UPDATE reel_posts SET tried_at = COALESCE(posted_at, created_at)"))

    # Collapse the history to one row per reel and platform, keeping the most
    # recent. From here on an attempt updates its row rather than adding one,
    # which is what makes the unique index below hold.
    op.execute(sa.text("""
        DELETE FROM reel_posts
        WHERE id NOT IN (
            SELECT MAX(id) FROM reel_posts GROUP BY reel_id, platform
        )
    """))
    op.create_index(
        "uq_reel_post_platform", "reel_posts", ["reel_id", "platform"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_reel_post_platform", table_name="reel_posts")
    op.drop_column("reel_posts", "tried_at")
