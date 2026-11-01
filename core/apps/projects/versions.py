"""项目保存记录及正式任务提交；不在 Backend 执行研究代码。"""
import json
import re
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import object_session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.tasks import services as task_service
from core.scheduler.client import DolphinSchedulerClient, DolphinSchedulerError

from .models import Project, ProjectDeletion, ProjectVersion
from .views import Database, get_project_or_404, project_directory

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
    session = object_session(version)
    if session is None:
        raise ValueError("任务状态同步需要数据库会话")
    kind = workflow_kind or session.get(Project, version.project_id).kind
    task_service.synchronize(session, version, kind)


def read_version(version: ProjectVersion) -> dict:
    reason = policy.version_retirement(version)
    from core.apps.artifacts.models import ResearchArtifact
    from core.apps.artifacts.service import read_artifact

    artifact = None
    session = object_session(version)
    snapshots = policy.snapshot_values(getattr(version, "accepted_artifacts", None))
    for snapshot in snapshots.values():
        if (snapshot.get("origin_project_id") != str(version.project_id)
                or snapshot.get("version") != version.package_version):
            continue
        if session is not None and snapshot.get("id"):
            artifact = session.get(ResearchArtifact, UUID(snapshot["id"]))
        break
    artifact_data = read_artifact(artifact) if artifact is not None else None
    project = session.get(Project, version.project_id)
    execution = task_service.record_summary(session, version, project.kind)
    return {
        "id": str(version.id), "number": version.number, "packageVersion": version.package_version,
        **execution,
        "retired": bool(reason), "retiredReason": reason,
        "note": version.note, "submittedAt": version.created_at.isoformat(),
        "status": "failed" if version.status == "submit_failed" else version.status if version.status in {"success", "failed", "stopped"} else "running",
        "phase": version.status, "publishStatus": "published" if artifact is not None and artifact.published_at else "unpublished",
        "artifactId": str(artifact.id) if artifact is not None else None, "artifact": artifact_data,
        "files": ([{"name": artifact.filename, "size": artifact.size_bytes,
                    "url": f"/api/v1/artifacts/{artifact.id}/wheel"}] if artifact is not None else []),
        "workflowId": version.workflow_id or 0,
        "error": version.error,
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
    if session.get(ProjectDeletion, project_id) is not None:
        raise HTTPException(409, "项目正在删除，不能保存新的研究版本")
    try:
        reason = policy.source_reason(policy.project_sources(project))
        if reason:
            raise policy.VersionPolicyError(reason)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    if session.scalar(select(ProjectVersion.id).where(ProjectVersion.project_id == project_id, ProjectVersion.status == "building")):
        raise HTTPException(409, "当前项目已有版本正在构建")
    # 删除版本后编号不复用，避免新版本与历史策略、报告中的 vN 及包版本混淆。
    latest = session.scalar(select(func.max(ProjectVersion.number)).where(ProjectVersion.project_id == project_id)) or 0
    number = max(project.version_sequence or 0, latest) + 1
    try:
        # 标签不代表实际版本；使用项目已解析的本地冻结环境，不访问远端。
        lock = tomllib.loads((project_directory(project) / "uv.lock").read_text(encoding="utf-8"))
        scheme = policy.require_project(project, lock=lock)
        policy.require_result_series(f"{scheme.major}.{scheme.minor}.{number}", scheme)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, "项目环境尚未记录有效的 Scheme 主、次版本") from error
    version = ProjectVersion(project_id=project_id, number=number, package_version=f"{scheme.major}.{scheme.minor}.{number}", note=body.note)
    session.add(version)
    project.version_sequence = number
    project.updated_at = datetime.now(UTC)
    session.commit()
    return read_version(version)


@router.post("/{project_id}/versions/{version_id}/submit")
def submit_version(project_id: UUID, version_id: UUID, body: BuildResult, session: Database) -> dict:
    version = session.scalar(select(ProjectVersion).where(ProjectVersion.id == version_id, ProjectVersion.project_id == project_id).with_for_update())
    if version is None:
        raise HTTPException(404, "版本不存在")
    if version.status in {"queued", "running", "stopping", "retrying"}:
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
    project = get_project_or_404(session, project_id)
    if session.get(ProjectDeletion, project_id) is not None:
        raise HTTPException(409, "项目正在删除，不能提交研究任务")
    try:
        reason = policy.source_reason(policy.project_sources(project)) or (
            policy.release_reason("scheme", version=version.scheme_version) if version.scheme_version else None
        )
        if reason:
            raise policy.VersionPolicyError(reason)
        directory = version_directory(version)
        kind = project.kind
        data = json.loads((directory / "input.json").read_text())
        if data["kind"] != kind:
            raise ValueError("研究类型与项目不一致")
        component = data["factor"] if kind == "factor" else data["algos"][project.kind]
        if component["version"] != version.package_version:
            raise ValueError("候选包版本或研究类型不一致")
        for file in [Path(component["wheel"]), Path(data["environment"]["lockfile"])]:
            file = directory / file
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
            directory / data["environment"]["lockfile"], component["package"],
        )
        _, _, frozen = policy.task_files(directory, kind)
        actual = policy.require_locked_scheme(frozen)
        policy.require_result_series(version.package_version, actual)
        built = json.loads((directory / "build.json").read_text())["scheme_version"]
        if policy.formal_version(built) != actual:
            raise ValueError("build.json ActualScheme 与冻结锁文件不一致")
        version.scheme_version = str(actual)
        if not version.accepted_artifacts:
            version.accepted_artifacts = policy.capture_task_artifacts(
                session, version, project=project, directory=directory, kind=kind,
            )
        policy.require_accepted_artifacts(version.accepted_artifacts, actual)
    except (OSError, ValueError, KeyError, TypeError) as error:
        if isinstance(error, policy.VersionPolicyError):
            detail, reason = error.detail, error.reason
        else:
            detail = reason = f"构建产物不完整：{error}"
        version.status, version.error = "failed", reason
        version.finished_at = datetime.now(UTC)
        session.commit()
        raise HTTPException(422, detail) from error
    # 先持久化提交意图；遇到网络超时仍按 input_file 查询实例，不盲目重试。
    version.status = "queued"
    version.error = ""
    version.finished_at = None
    attempt = task_service.current_attempt(session, version, kind)
    attempt.status, attempt.error = version.status, version.error
    attempt.sync_error = ""
    attempt.started_at = attempt.finished_at = attempt.synced_at = None
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


@router.delete("/{project_id}/versions/{version_id}")
def delete_version(project_id: UUID, version_id: UUID, session: Database) -> dict:
    """删除已结束且未发布的版本，并回收调度实例、run 目录和仅被它引用的候选包。"""
    from .deletion import delete_version as remove_version

    return remove_version(project_id, version_id, session)


@router.get("/{project_id}/versions")
def list_versions(project_id: UUID, session: Database) -> list[dict]:
    project = get_project_or_404(session, project_id)
    for version in project.versions:
        refresh_version(version, workflow_kind=project.kind)
    session.commit()
    return [read_version(version) for version in project.versions]
