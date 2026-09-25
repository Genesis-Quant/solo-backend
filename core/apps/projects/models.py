"""五类研究项目共用的项目记录。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        CheckConstraint("kind IN ('factor', 'model', 'optimize', 'control', 'execution')", name="ck_projects_kind"),
        Index("ix_projects_kind_archived_updated", "kind", "archived", "updated_at"),
        UniqueConstraint("kind", "name", name="uq_projects_kind_name"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4, comment="项目 ID，供 .solo 和插件关联项目")
    name: Mapped[str] = mapped_column(String(60), comment="项目名称")
    description: Mapped[str] = mapped_column(String(200), default="", comment="研究方向描述")
    kind: Mapped[str] = mapped_column(String(16), comment="项目类型：factor/model/optimize/control/execution")
    template_tag: Mapped[str] = mapped_column(String(128), comment="创建项目时选择的模板 Git Tag")
    template_commit: Mapped[str] = mapped_column(String(40), comment="实际使用的模板 Git commit SHA")
    scheme_version: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="创建项目时选择的 scheme Git Tag；旧项目未记录时为空")
    scheme_commit: Mapped[str | None] = mapped_column(String(40), nullable=True, comment="实际使用的 scheme Git commit SHA")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已删除隐藏；源码和成果保留")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, comment="更新时间（UTC）")
    versions: Mapped[list["ProjectVersion"]] = relationship(order_by="ProjectVersion.number.desc()", lazy="selectin")


class ProjectVersion(Base):
    __tablename__ = "project_versions"
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_project_version_number"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True, comment="所属项目")
    number: Mapped[int] = mapped_column(Integer, comment="项目内递增提交编号")
    package_version: Mapped[str] = mapped_column(String(128), comment="候选 wheel 的版本")
    note: Mapped[str] = mapped_column(String(500), default="", comment="版本备注")
    status: Mapped[str] = mapped_column(String(32), default="building", comment="building/queued/running/success/failed/submit_failed")
    workflow_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="DolphinScheduler 实例 ID")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, comment="冻结的输入参数")
    scheme_version: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="任务实际 Scheme 版本")
    error: Mapped[str] = mapped_column(Text, default="", comment="构建或运行错误")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
