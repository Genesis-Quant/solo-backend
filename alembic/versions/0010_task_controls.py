"""Track task retries without replacing frozen inputs or successful reports."""

import sqlalchemy as sa
from alembic import op

revision = "0010_task_controls"
down_revision = "0009_uuid_artifacts"
branch_labels = depends_on = None


def upgrade() -> None:
    for table in ("project_versions", "strategies"):
        op.add_column(table, sa.Column("attempt", sa.Integer(), server_default="1", nullable=False))
        op.add_column(table, sa.Column("scheduler_run_times", sa.Integer(), nullable=True))


def downgrade() -> None:
    for table in ("project_versions", "strategies"):
        op.drop_column(table, "scheduler_run_times")
        op.drop_column(table, "attempt")
