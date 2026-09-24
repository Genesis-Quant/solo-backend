"""Record scheme selection on projects; template_tag already records the Algo version."""

from alembic import op
import sqlalchemy as sa

revision = "0002_project_scheme"
down_revision = "0001_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("scheme_version", sa.String(128), nullable=True, comment="创建项目时选择的 scheme Git Tag；旧项目未记录时为空"))
    op.add_column("projects", sa.Column("scheme_commit", sa.String(40), nullable=True, comment="实际使用的 scheme Git commit SHA"))


def downgrade() -> None:
    op.drop_column("projects", "scheme_commit")
    op.drop_column("projects", "scheme_version")
