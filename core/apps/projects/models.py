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
    event,
    inspect,
    select,
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
    package_name: Mapped[str] = mapped_column(String(128), unique=True, comment="永久明确包身份，不随工作区名称或生命周期变化")
    artifact_dependencies: Mapped[dict] = mapped_column(JSON, default=dict, comment="已安装环境接受的独立 artifact 快照")
    description: Mapped[str] = mapped_column(String(200), default="", comment="研究方向描述")
    kind: Mapped[str] = mapped_column(String(16), comment="项目类型：factor/model/optimize/control/execution")
    template_tag: Mapped[str] = mapped_column(String(128), comment="创建项目时选择的模板 Git Tag")
    template_commit: Mapped[str] = mapped_column(String(40), comment="实际使用的模板 Git commit SHA")
    template_package_version: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="创建时验证的模板实际 package version，绑定 template_commit；旧记录为空")
    scheme_version: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="创建项目时选择的 scheme Git Tag；旧项目未记录时为空")
    scheme_commit: Mapped[str | None] = mapped_column(String(40), nullable=True, comment="实际使用的 scheme Git commit SHA")
    archived: Mapped[bool] = mapped_column(Boolean, default=False, comment="是否已删除隐藏；源码和成果保留")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, comment="创建时间（UTC）")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now, comment="更新时间（UTC）")
    versions: Mapped[list["ProjectVersion"]] = relationship(
        order_by="ProjectVersion.number.desc()", lazy="selectin", cascade="all, delete-orphan", passive_deletes=True,
    )

    def __init__(self, **kwargs):
        # Explicit identity is persisted; old constructor call sites preserve their
        # original kind/UUID-derived package rather than inventing a new name.
        kwargs.setdefault("id", uuid4())
        kwargs.setdefault("package_name", f"{kwargs['kind']}-{kwargs['id'].hex[:4]}")
        super().__init__(**kwargs)


class ProjectVersion(Base):
    __tablename__ = "project_versions"
    __table_args__ = (UniqueConstraint("project_id", "number", name="uq_project_version_number"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True, comment="所属项目")
    number: Mapped[int] = mapped_column(Integer, comment="项目内递增提交编号")
    package_version: Mapped[str] = mapped_column(String(128), comment="候选 wheel 的版本")
    note: Mapped[str] = mapped_column(String(500), default="", comment="版本备注")
    status: Mapped[str] = mapped_column(String(32), default="building", comment="building/queued/running/success/failed/submit_failed")
    workflow_id: Mapped[int | None] = mapped_column(Integer, nullable=True, comment="DolphinScheduler 实例 ID")
    input_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="提交前接受的准确输入哈希")
    lock_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, comment="提交前接受的准确锁文件哈希")
    accepted_artifacts: Mapped[dict | None] = mapped_column(JSON, default=dict, nullable=True, comment="任务接受时冻结的 package 到 artifact 快照；旧记录允许 NULL")
    parameters: Mapped[dict] = mapped_column(JSON, default=dict, comment="冻结的输入参数")
    scheme_version: Mapped[str | None] = mapped_column(String(128), nullable=True, comment="任务实际 Scheme 版本")
    error: Mapped[str] = mapped_column(Text, default="", comment="构建或运行错误")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def immutable_accepted_artifacts(mapper, connection, record) -> None:
    """A legacy NULL/empty manifest can be captured once; accepted truth cannot change."""
    if not inspect(record).attrs.accepted_artifacts.history.has_changes():
        return
    table = mapper.local_table
    previous = connection.execute(select(table.c.accepted_artifacts, table.c.input_sha256,
        table.c.lock_sha256).where(table.c.id == record.id)).one()
    if previous.accepted_artifacts and previous.input_sha256 and previous.lock_sha256:
        raise ValueError("Task accepted artifact snapshots are immutable after exact hash acceptance")


@event.listens_for(ProjectVersion, "before_update")
def immutable_project_version_artifacts(mapper, connection, record) -> None:
    immutable_accepted_artifacts(mapper, connection, record)


class ProjectDeletion(Base):
    """Retryable cleanup intent, removed after the original identity is cleaned."""

    __tablename__ = "project_deletions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, comment="原项目 UUID；不是新项目的外键")
    project_name: Mapped[str] = mapped_column(String(60))
    kind: Mapped[str] = mapped_column(String(16))
    delete_files: Mapped[bool] = mapped_column(Boolean, default=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, comment="可信暂存路径与清理进度")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
