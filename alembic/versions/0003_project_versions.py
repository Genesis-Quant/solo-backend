"""保存项目提交记录；源码、锁文件和报告保存在共享目录。"""
from alembic import op
import sqlalchemy as sa

revision = "0003_project_versions"
down_revision = "0002_project_scheme"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("package_version", sa.String(128), nullable=False, comment="候选 wheel 的版本"),
        sa.Column("note", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("parameters", sa.JSON(), nullable=False),
        sa.Column("scheme_version", sa.String(128), nullable=True),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("project_id", "number", name="uq_project_version_number"),
    )
    op.create_index("ix_project_versions_project_id", "project_versions", ["project_id"])


def downgrade() -> None:
    op.drop_table("project_versions")
