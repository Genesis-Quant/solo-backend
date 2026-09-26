"""项目保存记录及正式任务提交；不在 Backend 执行研究代码。"""
import hashlib
import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID
from zoneinfo import ZoneInfo

from config import DolphinSchedulerSettings, SoloSettings
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from core.scheduler.client import DolphinSchedulerClient, DolphinSchedulerError
from core.scheduler.task_logs import worker_task_log_page

from .models import Project, ProjectVersion
from .views import Database, get_project_or_404

router = APIRouter(prefix="/api/v1/projects", tags=["versions"])


def version_directory(version: ProjectVersion) -> Path:
    return SoloSettings.SHARED_DIR / "runs" / str(version.id)


def research_dependencies(lockfile: Path, current_package: str) -> list[dict[str, str]]:
    """从冻结环境记录所有上游研究包，包括 Model 间接依赖的 Factor。"""
    lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    stages = ("factor", "model", "optimize", "control", "execution")
    return sorted(
        [{"name": item["name"], "version": item["version"]}
         for item in lock.get("package", [])
         if item["name"] != current_package and re.fullmatch(
             r"(?:factor|model|optimize|control|execution)-[0-9a-f]{4}", item["name"]
         )],
        key=lambda item: (stages.index(item["name"].split("-", 1)[0]), item["name"]),
    )


def refresh_version(version: ProjectVersion, *, workflow_kind: str | None = None) -> None:
    if version.status not in {"queued", "running"}:
        return
    directory = version_directory(version)
    try:
        with DolphinSchedulerClient() as client:
            if not version.workflow_id:
                page = 1
                while True:
                    data = json.loads((directory / "input.json").read_text())
                    instances = client.instances(workflow_kind or data["kind"], page=page)
                    for instance in instances:
                        params = json.loads(instance.get("globalParams") or "[]")
                        if any(p.get("prop") == "input_file" and p.get("value") == str(directory / "input.json") for p in params):
                            version.workflow_id = instance["id"]
                            break
                    if version.workflow_id or len(instances) < 100:
                        break
                    page += 1
            if not version.workflow_id:
                return
            instance = client.instance(version.workflow_id)
    except (DolphinSchedulerError, OSError, ValueError, KeyError):
        return  # 调度服务短暂不可用时保留原状态。
    state = instance["state"]
    version.error = ""
    if state == "SUCCESS":
        try:
            run = json.loads((directory / "report/run.json").read_text())
            if run["status"] != "success" or run["input_sha256"] != hashlib.sha256((directory / "input.json").read_bytes()).hexdigest():
                raise ValueError("报告与提交输入不一致")
            version.scheme_version = run["versions"]["scheme"]
            version.status = "success"
        except (OSError, KeyError, ValueError) as error:
            version.status, version.error = "failed", f"报告清单无效：{error}"
    elif state in {"FAILURE", "STOP", "KILL", "PAUSE"}:
        version.status, version.error = "failed", f"DolphinScheduler：{state}，请查看任务日志"
    else:
        version.status = "running"
    if version.status in {"success", "failed"}:
        version.finished_at = datetime.now(UTC)


def read_version(version: ProjectVersion) -> dict:
    seconds = int(((version.finished_at or datetime.now(UTC)) - version.created_at).total_seconds())
    return {
        "id": str(version.id), "number": version.number, "packageVersion": version.package_version,
        "note": version.note, "submittedAt": version.created_at.isoformat(),
        "status": "failed" if version.status == "submit_failed" else version.status if version.status in {"success", "failed"} else "running",
        "phase": version.status, "publishStatus": "unpublished", "workflowId": version.workflow_id or 0,
        "duration": f"{seconds}s", "error": version.error,
        "reportPath": f"{version.id}/report" if version.status == "success" else None,
        "parameters": {k: json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v for k, v in version.parameters.items()},
        "dependencies": (
            ([{"name": "scheme", "version": version.scheme_version}] if version.scheme_version else [])
            + version.parameters.get("dependencies", [])
        ),
    }


class CreateVersion(BaseModel):
    note: str = Field(default="", max_length=500)


class BuildResult(BaseModel):
    error: str = Field(default="", max_length=10000)


def get_version(session: Database, project_id: UUID, version_id: UUID) -> ProjectVersion:
    version = session.get(ProjectVersion, version_id)
    if version is None or version.project_id != project_id:
        raise HTTPException(404, "版本不存在")
    return version


@router.post("/{project_id}/versions", status_code=201)
def create_version(project_id: UUID, body: CreateVersion, session: Database) -> dict:
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update())
    if project is None or project.archived:
        raise HTTPException(404, "项目不存在")
    if session.scalar(select(ProjectVersion.id).where(ProjectVersion.project_id == project_id, ProjectVersion.status == "building")):
        raise HTTPException(409, "当前项目已有版本正在构建")
    number = (session.scalar(select(func.max(ProjectVersion.number)).where(ProjectVersion.project_id == project_id)) or 0) + 1
    try:
        major = int((project.scheme_version or "").lstrip("v").split(".")[0])
    except ValueError as error:
        raise HTTPException(422, "项目尚未记录有效的 Scheme 版本") from error
    version = ProjectVersion(project_id=project_id, number=number, package_version=f"{major}.0.{number}", note=body.note)
    session.add(version)
    project.updated_at = datetime.now(UTC)
    session.commit()
    return read_version(version)


@router.post("/{project_id}/versions/{version_id}/submit")
def submit_version(project_id: UUID, version_id: UUID, body: BuildResult, session: Database) -> dict:
    version = session.scalar(select(ProjectVersion).where(ProjectVersion.id == version_id, ProjectVersion.project_id == project_id).with_for_update())
    if version is None:
        raise HTTPException(404, "版本不存在")
    if version.status in {"queued", "running"}:
        refresh_version(version, workflow_kind=session.get(Project, project_id).kind)
        session.commit()
        return read_version(version)
    if version.status not in {"building", "submit_failed"}:
        return read_version(version)  # 网络重试不重复提交工作流。
    if body.error:
        version.status, version.error = "failed", body.error
        version.finished_at = datetime.now(UTC)
        session.commit()
        return read_version(version)
    directory = version_directory(version)
    try:
        data = json.loads((directory / "input.json").read_text())
        project = session.get(Project, project_id)
        kind = project.kind
        if data["kind"] != kind:
            raise ValueError("研究类型与项目不一致")
        component = data["factor"] if kind == "factor" else data["algos"][project.kind]
        if component["version"] != version.package_version:
            raise ValueError("候选包版本或研究类型不一致")
        for file in [Path(component["wheel"]), Path(data["environment"]["lockfile"])]:
            if not file.resolve().is_relative_to(directory.resolve()) or not file.is_file():
                raise ValueError("任务产物不在版本目录中")
        version.parameters = (
            {"factor": component["params"], "analysis": data["analysis"]}
            if kind == "factor" else {"backtest": data["backtest"]}
        )
        if kind != "factor":
            version.parameters["algos"] = {
                name: {key: item[key] for key in ("package", "version", "entry") if key in item}
                for name, item in data["algos"].items() if item and name != project.kind
            }
        version.parameters["dependencies"] = research_dependencies(
            Path(data["environment"]["lockfile"]), component["package"],
        )
        version.scheme_version = json.loads((directory / "build.json").read_text())["scheme_version"]
    except (OSError, ValueError, KeyError) as error:
        raise HTTPException(422, f"构建产物不完整：{error}") from error
    # 先持久化提交意图；遇到网络超时仍按 input_file 查询实例，不盲目重试。
    version.status = "queued"
    version.error = ""
    version.finished_at = None
    session.commit()
    try:
        with DolphinSchedulerClient() as client:
            client.start(project.kind, str(directory / "input.json"))
    except DolphinSchedulerError as error:
        if error.submission_unknown:
            version.error = f"提交结果待核实：{error}"
        else:
            version.status = "submit_failed"
            version.finished_at = datetime.now(UTC)
            version.error = f"任务未提交，可重试：{error}"
        session.commit()
    return read_version(version)


@router.post("/{project_id}/versions/{version_id}/cancel")
def cancel_build(project_id: UUID, version_id: UUID, session: Database) -> dict:
    version = session.scalar(select(ProjectVersion).where(
        ProjectVersion.id == version_id, ProjectVersion.project_id == project_id,
    ).with_for_update())
    if version is None:
        raise HTTPException(404, "版本不存在")
    if version.status != "building":
        raise HTTPException(409, "该版本已结束构建，不能取消构建")
    version.status = "failed"
    version.error = "构建已取消，可重新保存版本"
    version.finished_at = datetime.now(UTC)
    session.commit()
    # 已启动的本地打包可能仍会结束；其迟到的 submit 请求不能再提交工作流。
    return read_version(version)


@router.get("/{project_id}/versions")
def list_versions(project_id: UUID, session: Database) -> list[dict]:
    project = get_project_or_404(session, project_id)
    for version in project.versions:
        refresh_version(version, workflow_kind=project.kind)
    session.commit()
    return [read_version(version) for version in project.versions]


@router.get("/{project_id}/versions/{version_id}/tasks")
def version_tasks(project_id: UUID, version_id: UUID, session: Database) -> dict:
    version = get_version(session, project_id, version_id)
    refresh_version(version, workflow_kind=session.get(Project, project_id).kind)
    session.commit()
    return workflow_tasks(version.workflow_id, version.status, version.error)


def workflow_tasks(workflow_id: int | None, status: str, error: str) -> dict:
    if not workflow_id:
        state = "FAILURE" if status in {"failed", "submit_failed"} else "WAIT_TO_RUN"
        return {"state": state, "error": error, "tasks": []}
    try:
        with DolphinSchedulerClient() as client:
            instance = client.instance(workflow_id)
            tasks = client.tasks(workflow_id)
    except DolphinSchedulerError as error:
        raise HTTPException(502, str(error)) from error
    return {
        "state": instance["state"], "error": error,
        "tasks": [{
            "task_instance_id": task["id"], "name": task["name"],
            "state": task["state"], "host": task.get("host"),
            "duration_seconds": task_duration(task),
        } for task in tasks],
    }


def task_duration(task: dict) -> float | None:
    if not task.get("startTime"):
        return None
    start = datetime.fromisoformat(task["startTime"])
    end = datetime.fromisoformat(task["endTime"]) if task.get("endTime") else datetime.now(UTC)
    # DS 返回的无偏移时间使用调度器时区，而非 Backend 所在机器的时区。
    timezone = ZoneInfo(DolphinSchedulerSettings.TIME_ZONE)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone)
    return max(0, (end.astimezone(UTC) - start.astimezone(UTC)).total_seconds())


def require_workflow_task(client: DolphinSchedulerClient, workflow_id: int | None, task_id: int) -> dict:
    tasks = client.tasks(workflow_id) if workflow_id else []
    task = next((item for item in tasks if item["id"] == task_id), None)
    if task is None:
        raise HTTPException(404, "该工作流中不存在此 Task")
    return task


@router.get("/{project_id}/versions/{version_id}/logs")
def version_logs(
    project_id: UUID, version_id: UUID, session: Database,
    task_instance_id: int,
    skip_line_num: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
    scope: Literal["full", "worker"] = "full",
    cursor: Annotated[str | None, Query(max_length=512)] = None,
) -> dict:
    version = get_version(session, project_id, version_id)
    return workflow_logs(version.workflow_id, task_instance_id, skip_line_num, limit, scope, cursor)


def workflow_logs(workflow_id: int | None, task_instance_id: int, skip_line_num: int = 0,
                  limit: int = 1000, scope: Literal["full", "worker"] = "full", cursor: str | None = None) -> dict:
    try:
        with DolphinSchedulerClient() as client:
            task = require_workflow_task(client, workflow_id, task_instance_id)
            if scope == "worker":
                page = worker_task_log_page(client, task_instance_id=task_instance_id,
                                            skip_line_num=skip_line_num, limit=limit, cursor=cursor)
            else:
                page = client.task_log(task_instance_id=task_instance_id, skip_line_num=skip_line_num, limit=limit)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except DolphinSchedulerError as error:
        raise HTTPException(502, str(error)) from error
    return {"workflow_instance_id": workflow_id, "task_instance_id": task_instance_id,
            "state": task["state"], "scope": scope, **page}


@router.get("/{project_id}/versions/{version_id}/logs/download", response_class=PlainTextResponse)
def download_version_log(project_id: UUID, version_id: UUID, task_instance_id: int, session: Database) -> str:
    version = get_version(session, project_id, version_id)
    return download_workflow_log(version.workflow_id, task_instance_id)


def download_workflow_log(workflow_id: int | None, task_instance_id: int) -> str:
    try:
        with DolphinSchedulerClient() as client:
            require_workflow_task(client, workflow_id, task_instance_id)
            return client.download_log(task_instance_id)
    except DolphinSchedulerError as error:
        raise HTTPException(502, str(error)) from error
