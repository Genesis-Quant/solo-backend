"""每轮运行独立保存计时、调度身份和日志来源。"""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Index, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.apps.projects.models import utc_now
from core.database.base import Base


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "number", name="uq_task_attempt_run_number"),
        Index("ix_task_attempt_run_created", "run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    run_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[int | None] = mapped_column(Integer)
    scheduler_run_times: Mapped[int | None] = mapped_column(Integer)
    baseline_task_id: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    scheduler_state: Mapped[str | None] = mapped_column(String(64))
    tasks: Mapped[list[dict]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")
    sync_error: Mapped[str] = mapped_column(Text, default="")
    control_action: Mapped[str | None] = mapped_column(String(16))
    retry_state: Mapped[str | None] = mapped_column(String(16))
    control_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
