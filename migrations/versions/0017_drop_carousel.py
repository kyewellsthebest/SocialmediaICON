"""Drop the second post.

Five reels a day, and that is the whole schedule. The two-slide post doubled
the grid with the same clip and cost four requests to a reel's two, which is
what spent Instagram's publishing allowance on a day four things had gone out.

The failed attempts go with it: they are noise from a feature that no longer
exists, and they would otherwise sit on the Posted tab naming a platform
nothing posts to. The ones that succeeded stay. Those are real posts that are
really on the account, and the row is the only record here of which reel they
belong to - which is the thing you need when somebody writes in asking for
their video to be taken down.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text(
        "DELETE FROM reel_posts "
        "WHERE platform = 'instagram_carousel' AND status <> 'posted'"
    ))
    for column in ("carousel_at", "carousel_note", "carousel_attempts",
                   "carousel_tried_at"):
        op.drop_column("reels", column)


def downgrade() -> None:
    op.add_column("reels", sa.Column("carousel_at", sa.DateTime(timezone=True)))
    op.add_column("reels", sa.Column("carousel_note", sa.Text()))
    op.add_column("reels", sa.Column(
        "carousel_attempts", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("reels", sa.Column("carousel_tried_at", sa.DateTime(timezone=True)))
