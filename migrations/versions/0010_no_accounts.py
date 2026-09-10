"""There is no table of accounts.

It was the thing stopping anything from posting: the destinations came from a
table of handles typed in by hand, and it sat empty next to a full set of Meta
credentials that already named every account perfectly well.

Two records of one fact is one too many. INSTAGRAM_USER_ID *is* the Instagram
account; a handle beside it adds nothing and can only ever disagree with it.
So the destinations are derived from the credentials that are set, and the way
to change where a post lands is to change the credential - which is also the
only way to change where a post can actually land.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text('DROP TABLE IF EXISTS "accounts" CASCADE'))


def downgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("handle", sa.String(length=120), nullable=False),
        sa.Column("auth_ref", sa.String(length=200)),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="active"),
    )
