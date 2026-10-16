"""Use UUID artifact identity and retain existing storage directory names."""

import sqlalchemy as sa
from alembic import op

revision = "0009_uuid_artifacts"
down_revision = "0008_artifact_install_seal"
branch_labels = depends_on = None


def upgrade() -> None:
    op.add_column("research_artifacts", sa.Column("storage_directory", sa.String(64), nullable=True))
    op.execute(sa.text("UPDATE research_artifacts SET storage_directory = sha256"))
    with op.batch_alter_table("research_artifacts") as batch:
        batch.alter_column("storage_directory", existing_type=sa.String(64), nullable=False)
        batch.drop_constraint("uq_artifacts_sha256", type_="unique")
        batch.drop_column("sha256")
        batch.drop_column("bundle_manifest_sha256")
    for table in ("project_versions", "strategies"):
        with op.batch_alter_table(table) as batch:
            batch.drop_column("input_sha256")
            batch.drop_column("lock_sha256")
    op.drop_table("artifact_installations")


def downgrade() -> None:
    raise RuntimeError("0009_uuid_artifacts cannot be downgraded: restoring hash identity would lose storage_directory mappings")
