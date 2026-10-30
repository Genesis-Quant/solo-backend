"""已删除版本的资源在后端自动重试，不作为版本记录返回 Web。"""

import asyncio
import logging

from sqlalchemy import select

from core.database.session import database_session_factory

from .deletion import delete_version
from .models import VersionDeletion

logger = logging.getLogger(__name__)


def retry_version_cleanups() -> None:
    factory = database_session_factory()
    with factory() as session:
        jobs = [(job.project_id, job.id) for job in session.scalars(select(VersionDeletion))
                if job.payload.get("phase") in {"deleted", "complete"}]
    for project_id, version_id in jobs:
        try:
            with factory() as session:
                delete_version(project_id, version_id, session)
        except Exception:
            logger.exception("后台版本资源清理未完成：%s", version_id)


async def run_version_cleanups(stopped: asyncio.Event) -> None:
    while not stopped.is_set():
        try:
            await asyncio.to_thread(retry_version_cleanups)
        except Exception:
            logger.exception("读取待清理版本失败，稍后自动重试")
        try:
            await asyncio.wait_for(stopped.wait(), timeout=30)
        except TimeoutError:
            continue
