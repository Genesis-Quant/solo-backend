"""Digest-addressed artifacts with no foreign keys to their descriptive origins."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, Text, Uuid, event, inspect, select, text
from sqlalchemy.orm import Mapped, mapped_column, synonym

from core.apps.projects.models import utc_now
from core.database.base import Base


class ResearchArtifact(Base):
    __tablename__ = "research_artifacts"
    __table_args__ = (
        CheckConstraint("kind IN ('factor', 'model', 'optimize', 'control', 'execution', 'dependency')", name="ck_artifacts_kind"),
        Index("uq_artifacts_published_package_version", "package_name", "version", unique=True,
              postgresql_where=text("published_at IS NOT NULL"), sqlite_where=text("published_at IS NOT NULL")),
        Index("ix_artifacts_kind_published", "kind", "published_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    kind: Mapped[str] = mapped_column(String(16))
    package_name: Mapped[str] = mapped_column(String(128))
    package = synonym("package_name")
    version: Mapped[str] = mapped_column(String(128))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    filename: Mapped[str] = mapped_column(String(255))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    entry: Mapped[str] = mapped_column(String(512), default="")
    scheme_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sources: Mapped[dict] = mapped_column(JSON, default=dict)
    origin_project_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    origin_project_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    origin_version_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    dependencies: Mapped[dict] = mapped_column(JSON, default=dict)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    bundle_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    bundle_manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ArtifactInstallation(Base):
    """Temporary consumer references, never a replacement for accepted snapshots."""

    __tablename__ = "artifact_installations"
    __table_args__ = (
        Index("ix_artifact_installations_consumer", "consumer_project_id"),
        Index("ix_artifact_installations_expiry", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    consumer_project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    artifacts: Mapped[dict] = mapped_column(JSON, default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(ResearchArtifact, "before_update")
def immutable_artifact(mapper, connection, artifact) -> None:
    state = inspect(artifact)
    mutable_on_publication = {"published_at", "bundle_path", "bundle_manifest_sha256", "dependencies"}
    changed = {attribute.key for attribute in state.attrs if attribute.history.has_changes()}
    if changed - mutable_on_publication:
        raise ValueError("Artifact identity, bytes and source snapshots are immutable")
    if not changed:
        return
    # Read persisted publication truth even when ORM attributes were expired or
    # assigned without loading their old values. A seal is never backfilled.
    previous = connection.execute(select(mapper.local_table.c.published_at,
        mapper.local_table.c.bundle_manifest_sha256).where(mapper.local_table.c.id == artifact.id)).one()
    if (previous.published_at is not None or previous.bundle_manifest_sha256 is not None
            or artifact.published_at is None or not artifact.bundle_path
            or not artifact.bundle_manifest_sha256 or len(artifact.bundle_manifest_sha256) != 64):
        raise ValueError("Artifact release closure and bundle can only be sealed once")
