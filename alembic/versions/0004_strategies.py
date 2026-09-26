"""独立保存策略组装与回测任务。"""
from alembic import op
import sqlalchemy as sa

revision = "0004_strategies"
down_revision = "0003_project_versions"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "strategies",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("forms", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("scheme_version", sa.String(128), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("strategies")
