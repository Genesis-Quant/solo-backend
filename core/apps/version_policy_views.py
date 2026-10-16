"""Public read-only policy and database-bound admission checks."""

from typing import Annotated, Literal, TypeAlias
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from core.apps import version_policy as policy
from core.apps.projects import deletion
from core.apps.projects.models import Project, ProjectDeletion, ProjectVersion
from core.apps.strategies.models import Strategy
from core.database.session import get_database_session

router = APIRouter(prefix="/api/v1/version-policy", tags=["version-policy"])
Database: TypeAlias = Annotated[Session, Depends(get_database_session)]


class TaskCheck(BaseModel):
    kind: Literal["factor", "model", "optimize", "control", "execution", "strategy"]


@router.get("")
def get_policy() -> dict:
    return policy.load_policy()


@router.post("/projects/{project_id}/check")
def check_project(project_id: UUID, session: Database) -> dict:
    if deletion.project_lifecycle_busy(project_id):
        raise HTTPException(409, {"code": "project_busy", "reason": "项目正在升级、修改或删除，请稍后重试"})
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if project.archived:
        raise HTTPException(422, {"code": "project_archived", "reason": "旧归档项目待清理，不能用于新研究"})
    if session.get(ProjectDeletion, project_id) is not None:
        raise HTTPException(409, {"code": "project_deleting", "reason": "项目正在删除"})
    try:
        policy.require_project(project)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    return {"allowed": True}


@router.post("/tasks/{run_id}/check")
def check_task(run_id: UUID, body: TaskCheck, session: Database) -> dict:
    record = session.get(Strategy if body.kind == "strategy" else ProjectVersion, run_id)
    if record is None:
        other = session.get(ProjectVersion if body.kind == "strategy" else Strategy, run_id)
        if other is not None:
            raise HTTPException(422, {"code": "task_identity_mismatch", "reason": "任务 kind 与数据库记录不一致"})
        raise HTTPException(404, {"code": "task_not_found", "reason": "任务不存在"})
    project = session.get(Project, record.project_id) if body.kind != "strategy" else None
    if body.kind != "strategy" and project is None:
        raise HTTPException(404, {"code": "task_not_found", "reason": "任务所属项目不存在"})
    try:
        policy.check_task(session, record, project=project, kind=body.kind)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, {"code": "task_identity_mismatch", "reason": str(error)}) from error
    return {"allowed": True}
