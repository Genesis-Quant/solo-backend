"""项目保存记录及正式任务提交；不在 Backend 执行研究代码。"""
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from config import SoloSettings
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from core.scheduler.client import DolphinSchedulerClient, DolphinSchedulerError

from .models import Project, ProjectVersion
from .views import Database, get_project_or_404

router = APIRouter(prefix="/api/v1/projects", tags=["versions"])


def version_directory(version: ProjectVersion) -> Path:
    return SoloSettings.SHARED_DIR / "runs" / str(version.id)


def refresh_version(version: ProjectVersion) -> None:
    if version.status not in {"queued", "running"}:
        return
    directory = version_directory(version)
    try:
        with DolphinSchedulerClient() as client:
            if not version.workflow_id:
                page = 1
                while True:
                    instances = client.instances("factor", page=page)
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
    except DolphinSchedulerError:
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
        "dependencies": [{"name": "scheme", "version": version.scheme_version}] if version.scheme_version else [],
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
    if project.kind != "factor":
        raise HTTPException(422, "当前保存入口支持因子项目")
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
        refresh_version(version)
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
        if data["factor"]["version"] != version.package_version or data["kind"] != "factor":
            raise ValueError("候选包版本或研究类型不一致")
        for file in [Path(data["factor"]["wheel"]), Path(data["environment"]["lockfile"])]:
            if not file.resolve().is_relative_to(directory.resolve()) or not file.is_file():
                raise ValueError("任务产物不在版本目录中")
        version.parameters = {"factor": data["factor"]["params"], "analysis": data["analysis"]}
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
            client.start("factor", str(directory / "input.json"))
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
        refresh_version(version)
    session.commit()
    return [read_version(version) for version in project.versions]


@router.get("/{project_id}/versions/{version_id}/logs")
def version_logs(project_id: UUID, version_id: UUID, session: Database) -> dict:
    version = get_version(session, project_id, version_id)
    refresh_version(version)
    session.commit()
    messages = []
    if version.workflow_id:
        try:
            with DolphinSchedulerClient() as client:
                for task in client.tasks(version.workflow_id):
                    messages.append(client.log(task["id"], limit=10000)["message"])
        except DolphinSchedulerError as error:
            raise HTTPException(502, str(error)) from error
    return {"message": "\n".join(messages) or version.error}
