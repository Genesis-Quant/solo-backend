"""Public read-only policy and database-bound admission checks."""

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from core.apps import version_policy as policy
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies.models import Strategy
from core.database.session import get_database_session

router = APIRouter(prefix="/api/v1/version-policy", tags=["version-policy"])
type Database = Annotated[Session, Depends(get_database_session)]


class TaskCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["factor", "model", "optimize", "control", "execution", "strategy"]
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    lock_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


@router.get("")
def get_policy() -> dict:
    return policy.load_policy()


@router.post("/projects/{project_id}/check")
def check_project(project_id: UUID, session: Database) -> dict:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    if project.archived:
        raise HTTPException(422, {"code": "project_archived", "reason": "归档项目仅允许读取历史"})
    try:
        policy.require_project(project)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    return {"allowed": True}


@router.post("/tasks/{run_id}/check")
def check_task(run_id: UUID, body: TaskCheck, session: Database) -> dict:
    version = session.get(ProjectVersion, run_id)
    strategy = session.get(Strategy, run_id)
    if (version is None) == (strategy is None):
        raise HTTPException(422, {"code": "task_not_found", "reason": "Run 必须对应唯一数据库任务记录"})
    record = version if version is not None else strategy
    project = session.get(Project, version.project_id) if version is not None else None
    if version is not None and project is None:
        raise HTTPException(422, {"code": "task_not_found", "reason": "任务所属项目不存在"})
    if body.kind != (project.kind if project is not None else "strategy"):
        raise HTTPException(422, {"code": "task_identity_mismatch", "reason": "任务 kind 与数据库记录不一致"})
    try:
        grandfathered = policy.check_task(session, record, project=project, kind=body.kind,
                                          input_sha256=body.input_sha256, lock_sha256=body.lock_sha256,
                                          require_accepted=True)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, {"code": "task_identity_mismatch", "reason": str(error)}) from error
    return {"allowed": True, "grandfathered": grandfathered}
