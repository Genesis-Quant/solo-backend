"""Bounded consumer install references and once-published bundle integrity seals.

Schema only. Existing published manifests are deliberately NOT trusted/backfilled;
without an original database seal they remain unavailable for trusted execution.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008_artifact_install_seal"
down_revision = "0007_artifact_registry"
branch_labels = depends_on = None


def upgrade() -> None:
    op.add_column("research_artifacts", sa.Column("bundle_manifest_sha256", sa.String(64), nullable=True))
    op.create_table("artifact_installations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("consumer_project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("artifacts", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_artifact_installations_consumer", "artifact_installations", ["consumer_project_id"])
    op.create_index("ix_artifact_installations_expiry", "artifact_installations", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_artifact_installations_expiry", table_name="artifact_installations")
    op.drop_index("ix_artifact_installations_consumer", table_name="artifact_installations")
    op.drop_table("artifact_installations")
    op.drop_column("research_artifacts", "bundle_manifest_sha256")
