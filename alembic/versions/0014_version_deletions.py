"""Persist version cleanup intent until its run resources have been reclaimed."""

import sqlalchemy as sa
from alembic import op

revision = "0014_version_deletions"
down_revision = "0013_project_version_sequence"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "version_deletions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_version_deletions_project_id", "version_deletions", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_version_deletions_project_id", table_name="version_deletions")
    op.drop_table("version_deletions")
