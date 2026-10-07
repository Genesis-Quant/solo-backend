"""Persist the exact task envelope accepted before scheduler submission."""

import sqlalchemy as sa
from alembic import op

revision = "0005_task_input_hashes"
down_revision = "0004_strategies"
branch_labels = depends_on = None


def upgrade() -> None:
    for table in ("project_versions", "strategies"):
        op.add_column(table, sa.Column("input_sha256", sa.String(64), nullable=True))
        op.add_column(table, sa.Column("lock_sha256", sa.String(64), nullable=True))


def downgrade() -> None:
    for table in ("strategies", "project_versions"):
        op.drop_column(table, "lock_sha256")
        op.drop_column(table, "input_sha256")
