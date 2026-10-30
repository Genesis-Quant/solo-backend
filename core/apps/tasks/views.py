"""任务列表、每轮执行详情、控制和日志读取。"""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import select

from core.apps.projects.models import Project, ProjectDeletion, ProjectVersion, VersionDeletion
from core.apps.projects.views import Database
from core.apps.strategies.models import Strategy
from core.scheduler.client import DolphinSchedulerClient, DolphinSchedulerError
from core.scheduler.task_logs import worker_task_log_page

from .models import TaskAttempt
from .services import control, frozen_parameters, list_item, resolve_record, summary, synchronize

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])


class TaskControl(BaseModel):
    attempt: int = Field(ge=1, strict=True)


@router.get("")
def list_tasks(session: Database) -> list[dict]:
    records = [(version, project, project.kind) for version, project in session.execute(
        select(ProjectVersion, Project).join(Project, Project.id == ProjectVersion.project_id)
        .where(Project.archived.is_(False)).order_by(ProjectVersion.created_at.desc())
    )]
    records.extend((strategy, None, "strategy") for strategy in session.scalars(select(Strategy)))
    records.sort(key=lambda item: item[0].created_at, reverse=True)
    result = []
    for record, project, kind in records:
        synchronize(session, record, kind)
        result.append(list_item(session, record, project, kind))
        session.commit()
    return result


@router.get("/{run_id}")
def task_details(run_id: UUID, session: Database) -> dict:
    record, project, kind = resolve_record(session, run_id)
    synchronize(session, record, kind)
    result = {**list_item(session, record, project, kind), "parameters": frozen_parameters(record, kind)}
    session.commit()
    return result


@router.post("/{run_id}/{action}")
def task_control(run_id: UUID, action: Literal["stop", "retry"], body: TaskControl, session: Database) -> dict:
    record, project, kind = resolve_record(session, run_id, lock=True)
    if project and session.get(VersionDeletion, run_id):
        raise HTTPException(409, "版本正在删除，不能执行任务控制")
    if project and action == "retry" and session.get(ProjectDeletion, project.id):
        raise HTTPException(409, "项目正在删除，不能重试任务")
    control(session, record, kind, action, body.attempt)
    result = list_item(session, record, project, kind)
    session.commit()
    return result


def require_attempt(session: Database, run_id: UUID, number: int) -> TaskAttempt:
    record, project, kind = resolve_record(session, run_id)
    if record.attempt == number:
        synchronize(session, record, kind)
    attempt = session.scalar(select(TaskAttempt).where(TaskAttempt.run_id == run_id, TaskAttempt.number == number))
    if attempt is None:
        raise HTTPException(404, "该轮运行记录不存在")
    return attempt


@router.get("/{run_id}/attempts/{number}/tasks")
def attempt_tasks(run_id: UUID, number: int, session: Database) -> dict:
    attempt = require_attempt(session, run_id, number)
    result = summary(attempt)
    session.commit()
    return {
        "state": attempt.scheduler_state or ("STOP" if attempt.status == "stopped" else "WAIT_TO_RUN"),
        "error": attempt.error or attempt.sync_error, "attempt": number,
        "duration_seconds": result["durationSeconds"], "started_at": result["startedAt"],
        "finished_at": result["finishedAt"], "tasks": result["tasks"],
    }


def require_log_task(attempt: TaskAttempt, task_id: int) -> dict:
    task = next((item for item in attempt.tasks if item["task_instance_id"] == task_id), None)
    if task is None:
        raise HTTPException(404, "该 Task 不属于选中的运行轮次")
    return task


@router.get("/{run_id}/attempts/{number}/logs")
def attempt_logs(
    run_id: UUID, number: int, session: Database, task_instance_id: int,
    skip_line_num: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
    scope: Literal["full", "worker"] = "full",
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict:
    attempt = require_attempt(session, run_id, number)
    task = require_log_task(attempt, task_instance_id)
    session.commit()
    try:
        with DolphinSchedulerClient() as client:
            page = worker_task_log_page(client, task_instance_id=task_instance_id, skip_line_num=skip_line_num,
                                        limit=limit, cursor=cursor) if scope == "worker" else client.task_log(
                                            task_instance_id=task_instance_id, skip_line_num=skip_line_num, limit=limit,
                                        )
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except DolphinSchedulerError as error:
        raise HTTPException(502, str(error)) from error
    return {"workflow_instance_id": attempt.workflow_id, "task_instance_id": task_instance_id,
            "state": task["state"], "scope": scope, **page}


@router.get("/{run_id}/attempts/{number}/logs/download", response_class=PlainTextResponse)
def download_attempt_log(run_id: UUID, number: int, session: Database, task_instance_id: int) -> str:
    attempt = require_attempt(session, run_id, number)
    require_log_task(attempt, task_instance_id)
    session.commit()
    try:
        with DolphinSchedulerClient() as client:
            return client.download_log(task_instance_id)
    except DolphinSchedulerError as error:
        raise HTTPException(502, str(error)) from error
