"""Keep project version numbers monotonic after versions are deleted."""

import sqlalchemy as sa
from alembic import op

revision = "0013_project_version_sequence"
down_revision = "0012_task_control_reconciliation"
branch_labels = depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "version_sequence", sa.Integer(), nullable=False, server_default="0",
            comment="已分配的最大版本编号；删除版本后不复用",
        ),
    )
    op.execute(sa.text("""
        UPDATE projects SET version_sequence = COALESCE(
            (SELECT MAX(number) FROM project_versions WHERE project_versions.project_id = projects.id), 0
        )
    """))


def downgrade() -> None:
    op.drop_column("projects", "version_sequence")
