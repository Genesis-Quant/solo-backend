"""Create research projects.

Revision ID: 0001_projects
Revises:
"""

from alembic import op
import sqlalchemy as sa

revision = "0001_projects"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True, comment="项目 ID，供 .solo 和插件关联项目"),
        sa.Column("name", sa.String(60), nullable=False, comment="项目名称"),
        sa.Column("description", sa.String(200), nullable=False, comment="研究方向描述"),
        sa.Column("kind", sa.String(16), nullable=False, comment="项目类型：factor/model/optimize/control/execution"),
        sa.Column("template_tag", sa.String(128), nullable=False, comment="创建项目时选择的模板 Git Tag"),
        sa.Column("template_commit", sa.String(40), nullable=False, comment="实际使用的模板 Git commit SHA"),
        sa.Column("archived", sa.Boolean(), nullable=False, comment="是否已删除隐藏；源码和成果保留"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, comment="创建时间（UTC）"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, comment="更新时间（UTC）"),
        sa.CheckConstraint("kind IN ('factor', 'model', 'optimize', 'control', 'execution')", name="ck_projects_kind"),
        sa.UniqueConstraint("kind", "name", name="uq_projects_kind_name"),
    )
    op.create_index("ix_projects_kind_archived_updated", "projects", ["kind", "archived", "updated_at"])


def downgrade() -> None:
    op.drop_table("projects")
