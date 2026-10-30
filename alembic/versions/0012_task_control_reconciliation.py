"""Persist retry acceptance separately from cancellation intent."""

import sqlalchemy as sa
from alembic import op

revision = "0012_task_control_reconciliation"
down_revision = "0011_task_attempts"
branch_labels = depends_on = None


def upgrade() -> None:
    op.add_column("task_attempts", sa.Column("retry_state", sa.String(16), nullable=True))
    op.add_column("task_attempts", sa.Column("control_requested_at", sa.DateTime(timezone=True), nullable=True))
    op.execute(sa.text("""
        UPDATE task_attempts SET retry_state = 'legacy', control_requested_at = created_at
        WHERE status = 'retrying' OR (status = 'stopping' AND baseline_task_id > 0)
    """))


def downgrade() -> None:
    op.drop_column("task_attempts", "control_requested_at")
    op.drop_column("task_attempts", "retry_state")
