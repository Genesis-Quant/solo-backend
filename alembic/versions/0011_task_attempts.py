"""Keep task timing and log identity separately for every execution attempt."""

import sqlalchemy as sa
from alembic import op

revision = "0011_task_attempts"
down_revision = "0010_task_controls"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "task_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("workflow_id", sa.Integer()),
        sa.Column("scheduler_run_times", sa.Integer()),
        sa.Column("baseline_task_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("scheduler_state", sa.String(64)),
        sa.Column("tasks", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("sync_error", sa.Text(), nullable=False),
        sa.Column("control_action", sa.String(16)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("synced_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "number", name="uq_task_attempt_run_number"),
    )
    op.create_index("ix_task_attempt_run_created", "task_attempts", ["run_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_task_attempt_run_created", table_name="task_attempts")
    op.drop_table("task_attempts")
