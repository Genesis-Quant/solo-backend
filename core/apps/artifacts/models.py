"""UUID-addressed artifacts with descriptive origins independent of source projects."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, Index, String, Text, Uuid, text
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
    storage_directory: Mapped[str] = mapped_column(String(64))
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
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
