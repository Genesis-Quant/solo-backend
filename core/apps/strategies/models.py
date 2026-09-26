from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from core.apps.projects.models import utc_now
from core.database.base import Base


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(60), comment="组装策略名称")
    components: Mapped[dict] = mapped_column(JSON, comment="各环节选定的项目版本或默认算法")
    forms: Mapped[dict] = mapped_column(JSON, comment="各环节填写的 Form 参数")
    status: Mapped[str] = mapped_column(String(32), default="building")
    workflow_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheme_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
