"""Independent immutable artifacts, frozen task snapshots and true deletion jobs.

Schema only: resource snapshot backfill and archived cleanup are explicit backed-up
operations, never a side effect of applying a migration or starting the server.
"""

import sqlalchemy as sa
from alembic import op

revision = "0007_artifact_registry"
down_revision = "0006_project_template_version"
branch_labels = depends_on = None


def upgrade() -> None:
    op.create_table("research_artifacts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("package_name", sa.String(128), nullable=False),
        sa.Column("version", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("entry", sa.String(512), nullable=False),
        sa.Column("scheme_version", sa.String(128), nullable=True),
        sa.Column("sources", sa.JSON(), nullable=False),
        sa.Column("origin_project_id", sa.Uuid(), nullable=True),
        sa.Column("origin_project_name", sa.String(60), nullable=True),
        sa.Column("origin_version_id", sa.Uuid(), nullable=True),
        sa.Column("dependencies", sa.JSON(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bundle_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('factor', 'model', 'optimize', 'control', 'execution', 'dependency')", name="ck_artifacts_kind"),
        sa.UniqueConstraint("sha256", name="uq_artifacts_sha256"))
    op.create_index("uq_artifacts_published_package_version", "research_artifacts", ["package_name", "version"], unique=True,
        postgresql_where=sa.text("published_at IS NOT NULL"), sqlite_where=sa.text("published_at IS NOT NULL"))
    op.create_index("ix_artifacts_kind_published", "research_artifacts", ["kind", "published_at"])
    op.create_table("project_deletions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("project_name", sa.String(60), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("delete_files", sa.Boolean(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.add_column("projects", sa.Column("package_name", sa.String(128), nullable=True))
    op.add_column("projects", sa.Column("artifact_dependencies", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    # This preserves the exact old on-disk package identity; no mutable workspace
    # inspection, guessed release sources or invented accepted hashes.
    connection = op.get_bind()
    dialect = connection.dialect.name
    uuid_text = "CAST(id AS TEXT)" if dialect != "postgresql" else "id::text"
    connection.execute(sa.text(f"UPDATE projects SET package_name = kind || '-' || substr(replace({uuid_text}, '-', ''), 1, 4)"))
    naming = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
    with op.batch_alter_table("projects") as batch:
        batch.alter_column("package_name", existing_type=sa.String(128), nullable=False)
        batch.create_unique_constraint("uq_projects_package_name", ["package_name"])
        batch.alter_column("artifact_dependencies", existing_type=sa.JSON(), server_default=None)
    op.add_column("project_versions", sa.Column("accepted_artifacts", sa.JSON(), nullable=True))
    op.add_column("strategies", sa.Column("accepted_artifacts", sa.JSON(), nullable=True))
    # Leave legacy task snapshots NULL until actual frozen bytes are imported.
    foreign_keys = sa.inspect(connection).get_foreign_keys("project_versions")
    parent_key = next(key for key in foreign_keys if key["constrained_columns"] == ["project_id"])
    with op.batch_alter_table("project_versions", naming_convention=naming) as batch:
        batch.drop_constraint(parent_key["name"] or "fk_project_versions_project_id_projects", type_="foreignkey")
        batch.create_foreign_key("fk_project_versions_project_id_projects", "projects", ["project_id"], ["id"], ondelete="CASCADE")


def downgrade() -> None:
    naming = {"fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s"}
    with op.batch_alter_table("project_versions", naming_convention=naming) as batch:
        batch.drop_constraint("fk_project_versions_project_id_projects", type_="foreignkey")
        batch.create_foreign_key("fk_project_versions_project_id_projects", "projects", ["project_id"], ["id"])
        batch.drop_column("accepted_artifacts")
    op.drop_column("strategies", "accepted_artifacts")
    with op.batch_alter_table("projects") as batch:
        batch.drop_constraint("uq_projects_package_name", type_="unique")
        batch.drop_column("artifact_dependencies")
        batch.drop_column("package_name")
    op.drop_table("project_deletions")
    op.drop_index("ix_artifacts_kind_published", table_name="research_artifacts")
    op.drop_index("uq_artifacts_published_package_version", table_name="research_artifacts")
    op.drop_table("research_artifacts")
