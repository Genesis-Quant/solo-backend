from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from config import SoloSettings
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from core.apps import version_policy as policy
from core.apps.artifacts.service import lock_registry
from core.apps.projects.versions import (
    download_workflow_log,
    refresh_version,
    workflow_logs,
    workflow_tasks,
)
from core.apps.projects.views import Database
from core.scheduler.client import DolphinSchedulerClient, DolphinSchedulerError

from .models import Strategy
from .service import KINDS, ComponentInspector, build, selected

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: UUID | None = None
    optimize: UUID | None = None
    control: UUID | None = None
    execution: UUID | None = None
    artifacts: dict[Literal["model", "optimize", "control", "execution"], UUID] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_references(self):
        for kind in self.artifacts:
            if getattr(self, kind) is not None:
                raise ValueError(f"{kind} 不能同时选择研究版本和已发布成果")
        if self.model is None and "model" not in self.artifacts:
            raise ValueError("请选择 Model 研究版本或已发布成果")
        return self


class CreateStrategy(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    selections: Selection
    forms: dict[str, dict]


def read(strategy: Strategy) -> dict:
    reason = policy.version_retirement(strategy)
    return {"id": str(strategy.id), "name": strategy.name, "components": strategy.components,
            "retired": bool(reason), "retiredReason": reason,
            "forms": strategy.forms, "status": strategy.status, "error": strategy.error,
            "workflowId": strategy.workflow_id, "schemeVersion": strategy.scheme_version,
            "createdAt": strategy.created_at.isoformat(),
            "duration": f"{max(0, int(((strategy.finished_at or datetime.now(UTC)) - strategy.created_at).total_seconds()))}s",
            "reportPath": f"{strategy.id}/report" if strategy.status == "success" else None}


@router.get("")
def list_strategies(session: Database) -> list[dict]:
    strategies = list(session.scalars(select(Strategy).order_by(Strategy.created_at.desc())))
    for strategy in strategies:
        refresh_version(strategy, workflow_kind="strategy")
    session.commit()
    return [read(strategy) for strategy in strategies]


@router.post("/forms")
def forms(body: Selection, session: Database) -> dict:
    try:
        lock_registry(session)
        components = selected(session, body.model_dump())
        inspector = ComponentInspector(components)
        return {kind: inspector.inspect(kind) for kind in KINDS}
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(422, str(error)) from error


@router.post("", status_code=201)
def create_strategy(body: CreateStrategy, session: Database) -> dict:
    if not body.name.strip() or set(body.forms) != set(KINDS):
        raise HTTPException(422, "请填写策略名称和全部环节的 Form")
    try:
        lock_registry(session)
        components = selected(session, body.selections.model_dump())
        descriptors = {}
        for kind, item in components.items():
            descriptor = {key: value for key, value in item.items()
                          if key not in {"component", "input", "environment_path"}}
            if item.get("component"):
                descriptor["component_snapshot"] = {key: item["component"][key]
                                                    for key in ("package", "version", "entry")}
            descriptors[kind] = descriptor
        strategy = Strategy(name=body.name.strip(), forms=body.forms, components=descriptors)
        session.add(strategy)
        # 冻结副本及接受快照完成前不能提交，否则会提前释放成果仓库锁。
        session.flush()
        directory = SoloSettings.SHARED_DIR / "runs" / str(strategy.id)
        try:
            strategy.scheme_version = build(components, body.forms, directory)
            strategy.accepted_artifacts = policy.capture_task_artifacts(
                session, strategy, kind="strategy", directory=directory,
            )
            strategy.status = "queued"
            session.commit()
            with DolphinSchedulerClient() as client:
                client.start("strategy", str(directory / "input.json"))
        except (OSError, ValueError, DolphinSchedulerError) as error:
            if isinstance(error, DolphinSchedulerError) and error.submission_unknown:
                strategy.error = "调度响应超时，正在确认任务状态"
            else:
                strategy.status, strategy.error = "failed", str(error)
                strategy.finished_at = datetime.now(UTC)
        session.commit()
        return read(strategy)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except (ValueError, OSError, KeyError, TypeError) as error:
        raise HTTPException(422, str(error)) from error


@router.get("/{identifier}")
def get_strategy(identifier: UUID, session: Database) -> dict:
    strategy = require_strategy(identifier, session)
    refresh_version(strategy, workflow_kind="strategy")
    session.commit()
    return read(strategy)


def require_strategy(identifier: UUID, session: Database) -> Strategy:
    strategy = session.get(Strategy, identifier)
    if strategy is None:
        raise HTTPException(404, "策略不存在")
    return strategy


@router.get("/{identifier}/tasks")
def strategy_tasks(identifier: UUID, session: Database) -> dict:
    strategy = require_strategy(identifier, session)
    refresh_version(strategy, workflow_kind="strategy")
    session.commit()
    return workflow_tasks(strategy.workflow_id, strategy.status, strategy.error)


@router.get("/{identifier}/logs")
def strategy_logs(
    identifier: UUID, session: Database, task_instance_id: int,
    skip_line_num: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
    scope: Literal["full", "worker"] = "full",
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict:
    strategy = require_strategy(identifier, session)
    return workflow_logs(strategy.workflow_id, task_instance_id, skip_line_num, limit, scope, cursor)


@router.get("/{identifier}/logs/download", response_class=PlainTextResponse)
def strategy_log_download(identifier: UUID, session: Database, task_instance_id: int) -> str:
    return download_workflow_log(require_strategy(identifier, session).workflow_id, task_instance_id)
