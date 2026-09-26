from datetime import UTC, datetime
from uuid import UUID
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from config import SoloSettings
from core.apps.projects.versions import refresh_version, workflow_tasks, workflow_logs, download_workflow_log
from core.apps.projects.views import Database
from core.scheduler.client import DolphinSchedulerClient, DolphinSchedulerError

from .models import Strategy
from .service import KINDS, build, inspect_component, selected

router = APIRouter(prefix="/api/v1/strategies", tags=["strategies"])


class Selection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: UUID
    optimize: UUID | None = None
    control: UUID | None = None
    execution: UUID | None = None


class CreateStrategy(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    selections: Selection
    forms: dict[str, dict]


def read(strategy: Strategy) -> dict:
    return {"id": str(strategy.id), "name": strategy.name, "components": strategy.components,
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
        components = selected(session, body.model_dump())
        return {kind: inspect_component(components, kind) for kind in KINDS}
    except (ValueError, OSError) as error:
        raise HTTPException(422, str(error)) from error


@router.post("", status_code=201)
def create_strategy(body: CreateStrategy, session: Database) -> dict:
    if not body.name.strip() or set(body.forms) != set(KINDS):
        raise HTTPException(422, "请填写策略名称和全部环节的 Form")
    try:
        components = selected(session, body.selections.model_dump())
        strategy = Strategy(name=body.name.strip(), forms=body.forms, components={
            kind: {key: value for key, value in item.items() if key not in {"component", "input"}}
            for kind, item in components.items()
        })
        session.add(strategy)
        session.commit()
        directory = SoloSettings.SHARED_DIR / "runs" / str(strategy.id)
        try:
            strategy.scheme_version = build(components, body.forms, directory)
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
    except (ValueError, OSError) as error:
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
