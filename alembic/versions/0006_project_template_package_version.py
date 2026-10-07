"""Keep the verified, commit-bound template package version for new projects."""

import sqlalchemy as sa

from alembic import op

revision = "0006_project_template_version"
down_revision = "0005_task_input_hashes"
branch_labels = depends_on = None


def upgrade() -> None:
    # Historical records stay NULL; a Git tag is not evidence of package metadata.
    op.add_column("projects", sa.Column("template_package_version", sa.String(128), nullable=True,
                                     comment="创建时验证的模板实际 package version，绑定 template_commit；旧记录为空"))


def downgrade() -> None:
    op.drop_column("projects", "template_package_version")
