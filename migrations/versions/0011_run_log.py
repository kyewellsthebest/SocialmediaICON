"""Record what each run did, whether or not it worked.

The daily pass moved into the web service, where it runs on a thread. A thread
has nobody waiting on it and no response to fail, so a run that throws is
completely silent - and an empty queue then means either "nothing was found",
"it never started" or "it crashed", which are three different things to go and
fix.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "run_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True),
                  server_default=sa.func.now(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("added", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pushed_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("posted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
    )
    # Every read of this table is "the most recent run".
    op.create_index("ix_run_log_started", "run_log", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_run_log_started", table_name="run_log")
    op.drop_table("run_log")
