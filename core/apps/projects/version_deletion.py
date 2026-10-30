"""可重试的版本清理；只回收原 Run，不触碰项目源码、Kernel 或独立下游。"""

import json
import logging
import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from config import DolphinSchedulerSettings, SoloSettings
from core.apps.artifacts.models import ResearchArtifact
from core.apps.artifacts.service import gc_unreferenced, lock_registry
from core.apps.strategies.models import Strategy
from core.apps.tasks.models import TaskAttempt
from core.scheduler.client import DolphinSchedulerBusyError, DolphinSchedulerClient

from .deletion import ACTIVE_PHASES, acquire_lease, directory_size, release_lease, version_published
from .models import Project, ProjectDeletion, ProjectVersion, VersionDeletion

logger = logging.getLogger(__name__)


def owned_directory(path: Path, root: Path) -> bool:
    if root.is_symlink() or root.is_junction() or root.resolve() != SoloSettings.SHARED_DIR.resolve() / "runs":
        raise HTTPException(409, "Run 根目录被重定向，未执行清理")
    if path.parent != root or path.is_symlink() or path.is_junction():
        raise HTTPException(409, "Run 清理路径不能是链接或越界目录")
    if path.exists() and not path.is_dir():
        raise HTTPException(409, "Run 清理目标不是目录")
    return path.is_dir()


def paths(job: VersionDeletion) -> tuple[Path, Path]:
    root = SoloSettings.SHARED_DIR / "runs"
    return root / str(job.id), root / f".delete-version-{job.id}"


def marker(job: VersionDeletion) -> dict:
    return {"version_id": str(job.id), "project_id": str(job.project_id), "number": job.number, "kind": job.kind}


def stage_exists(job: VersionDeletion) -> bool:
    run, stage = paths(job)
    if not owned_directory(stage, run.parent):
        return False
    if not any(stage.iterdir()):
        return True
    metadata = stage / "metadata.json"
    try:
        if metadata.is_symlink() or not metadata.is_file():
            raise ValueError("missing marker")
        identity = json.loads(metadata.read_text(encoding="utf-8"))
        if identity != marker(job) or {p.name for p in stage.iterdir()} - {"metadata.json", "run"}:
            raise ValueError("wrong owner")
        payload = stage / "run"
        if payload.is_symlink() or payload.is_junction() or (payload.exists() and not payload.is_dir()):
            raise ValueError("invalid payload")
    except (OSError, ValueError, TypeError) as error:
        raise HTTPException(409, "Run 清理暂存目录身份无效，未执行删除") from error
    return True


def stage_run(job: VersionDeletion) -> None:
    run, stage = paths(job)
    exists = stage_exists(job)
    source = owned_directory(run, run.parent)
    if exists and (stage / "run").exists():
        if source:
            raise HTTPException(409, "Run 与清理暂存目录冲突")
        return
    if not source:
        return
    input_file = run / "input.json"
    if input_file.exists():
        if input_file.is_symlink():
            raise HTTPException(409, "Run 输入文件不能是链接")
        try:
            data = json.loads(input_file.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("kind") != job.kind:
                raise ValueError("wrong kind")
        except (OSError, ValueError) as error:
            raise HTTPException(409, "Run 输入不属于该版本的研究类型") from error
    stage.mkdir(exist_ok=True)
    (stage / "metadata.json").write_text(json.dumps(marker(job), ensure_ascii=False), encoding="utf-8")
    run.rename(stage / "run")


def restore_run(job: VersionDeletion) -> None:
    run, stage = paths(job)
    if not stage_exists(job):
        return
    payload = stage / "run"
    if payload.exists():
        if owned_directory(run, run.parent):
            raise HTTPException(409, "Run 目录已存在，不能覆盖恢复")
        payload.rename(run)
    (stage / "metadata.json").unlink(missing_ok=True)
    stage.rmdir()


def purge_run(job: VersionDeletion) -> None:
    run, stage = paths(job)
    if owned_directory(run, run.parent):
        raise HTTPException(409, "版本已删除但原 Run 路径被重新创建，拒绝清理未知目录")
    if stage_exists(job):
        payload = stage / "run"
        if payload.exists():
            shutil.rmtree(payload)
        (stage / "metadata.json").unlink(missing_ok=True)
        stage.rmdir()


def artifact_candidates(session: Session, version: ProjectVersion) -> list[str]:
    from core.apps.artifacts.service import _snapshot_ids

    retained = set(_snapshot_ids(version.accepted_artifacts))
    artifacts = list(session.scalars(select(ResearchArtifact)))
    retained.update(str(a.id) for a in artifacts if a.origin_version_id == version.id)
    by_id = {str(a.id): a for a in artifacts}
    pending = list(retained)
    while pending:
        artifact = by_id.get(pending.pop())
        if artifact is not None:
            for identifier in set(_snapshot_ids(artifact.dependencies)) - retained:
                retained.add(identifier)
                pending.append(identifier)
    return sorted(retained)


def require_deletable(session: Session, project: Project, version: ProjectVersion) -> list[TaskAttempt]:
    attempts = list(session.scalars(select(TaskAttempt).where(TaskAttempt.run_id == version.id)))
    if version.status in ACTIVE_PHASES or any(a.status in ACTIVE_PHASES for a in attempts):
        raise HTTPException(409, "版本正在构建或运行，请先终止任务或取消构建")
    if version_published(session, project, version):
        raise HTTPException(409, "该版本已发布为独立成果，不能删除")
    if session.get(Strategy, version.id) is not None:
        raise HTTPException(409, "研究 Run 与独立策略 Run 身份冲突，未删除任何文件")
    return attempts


def prepare_delete(job: VersionDeletion, session: Session) -> None:
    lock_registry(session)  # 与成果发布、下游接受及策略冻结保持相同锁顺序。
    project = session.scalar(select(Project).where(Project.id == job.project_id).with_for_update()
                             .execution_options(populate_existing=True))
    if project is None or project.archived or session.get(ProjectDeletion, job.project_id):
        raise HTTPException(409, "项目不存在或正在删除，不能单独删除版本")
    version = session.scalar(select(ProjectVersion).where(ProjectVersion.id == job.id).with_for_update()
                             .execution_options(populate_existing=True))
    if version is None or version.project_id != job.project_id:
        raise HTTPException(404, "版本不存在")
    attempts = require_deletable(session, project, version)
    run, stage = paths(job)
    owned_directory(run, run.parent)
    stage_exists(job)
    input_file = str(run / "input.json")
    known = {a.workflow_id: input_file for a in attempts if a.workflow_id}
    if version.workflow_id:
        known[version.workflow_id] = input_file
    if DolphinSchedulerSettings.ENABLED:
        with DolphinSchedulerClient() as client:
            instances = client.owned_instances(job.kind, {input_file}, known)
    elif known:
        raise HTTPException(503, "DolphinScheduler 不可用，无法确认该版本已结束")
    else:
        instances = []
    stage_run(job)
    payload = stage / "run"
    job.payload = {**job.payload, "phase": "deleted", "instances": instances,
                   "artifact_ids": artifact_candidates(session, version),
                   "run_bytes": directory_size(payload) if payload.is_dir() else 0, "freed_bytes": 0}
    project.version_sequence = max(project.version_sequence or 0, version.number)
    project.updated_at = datetime.now(UTC)
    session.execute(delete(TaskAttempt).where(TaskAttempt.run_id == version.id))
    session.delete(version)
    session.commit()


def reclaim(job: VersionDeletion, session: Session) -> None:
    if job.payload.get("phase") == "complete":
        return
    instances = list(job.payload.get("instances", []))
    if instances:
        with DolphinSchedulerClient() as client:
            for instance in instances:
                client.delete_owned_instance(instance)
                job.payload = {**job.payload, "instances": [i for i in job.payload["instances"] if i != instance]}
                session.commit()
    if not job.payload.get("run_purged"):
        purge_run(job)
        job.payload = {**job.payload, "run_purged": True,
                       "freed_bytes": job.payload.get("freed_bytes", 0) + job.payload.get("run_bytes", 0)}
        session.commit()
    if not job.payload.get("artifacts_purged"):
        result = gc_unreferenced(session, candidates=set(job.payload.get("artifact_ids", [])))
        job.payload = {**job.payload, "artifacts_purged": True,
                       "freed_bytes": job.payload.get("freed_bytes", 0) + result["freedBytes"]}
        session.commit()
    job.payload = {**job.payload, "phase": "complete"}
    job.error = ""
    session.commit()


def remove_version(project_id: UUID, version_id: UUID, session: Session) -> dict:
    lock_registry(session)
    job = session.get(VersionDeletion, version_id)
    if job is not None and job.project_id != project_id:
        raise HTTPException(404, "版本不存在")
    if job is None:
        project = session.get(Project, project_id)
        version = session.get(ProjectVersion, version_id)
        if project is None or project.archived or version is None or version.project_id != project_id:
            raise HTTPException(404, "版本不存在")
        require_deletable(session, project, version)
        job = VersionDeletion(id=version.id, project_id=project.id, number=version.number, kind=project.kind,
                              payload={"phase": "preparing", "lease": str(uuid4())}, error="")
        session.add(job)
        session.commit()  # lease 在请求 Jupyter 前持久化，丢失响应时仍可按同一 nonce 恢复。
    number = job.number
    lease = job.payload["lease"]
    session.commit()
    rejected = False
    failure = None
    try:
        try:
            acquire_lease(project_id, lease)
        except HTTPException as error:
            rejected = error.status_code == 409
            raise
        if job.payload.get("phase") == "preparing":
            prepare_delete(job, session)
        reclaim(job, session)
    except Exception as error:
        session.rollback()
        persisted = session.get(VersionDeletion, version_id, populate_existing=True)
        version_exists = session.get(ProjectVersion, version_id, populate_existing=True) is not None
        if persisted is None:
            raise HTTPException(503, "无法确认删除提交状态，请按原版本重试") from error
        job = persisted
        if version_exists:
            restore_run(job)
            if isinstance(error, DolphinSchedulerBusyError):
                failure = HTTPException(409, "该版本在调度器中仍未结束，不能删除")
            elif isinstance(error, HTTPException):
                failure = error
            else:
                failure = HTTPException(503, "版本删除未完成，原 Run 已恢复，请重试")
        else:
            failure = HTTPException(503, "版本记录已删除，资源清理未完成，请重试清理")
        job.error = str(failure.detail)
        session.commit()
        logger.warning("版本 %s 清理未完成：%s", version_id, job.error, exc_info=True)
    finally:
        if not rejected:
            try:
                release_lease(project_id, lease)
            except HTTPException:
                job.error = "项目清理锁尚未释放，请按原版本重试清理"
                session.commit()
                failure = HTTPException(503, job.error)
    deleted = session.get(ProjectVersion, version_id) is None
    if failure is not None:
        if deleted:
            return {"id": str(version_id), "number": number, "freedBytes": job.payload.get("freed_bytes", 0),
                    "cleanupPending": True, "error": job.error}
        raise failure
    freed = job.payload.get("freed_bytes", 0)
    session.delete(job)
    session.commit()
    return {"id": str(version_id), "number": number, "freedBytes": freed, "cleanupPending": False}
