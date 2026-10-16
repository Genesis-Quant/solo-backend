"""Delete only an artifact's owned directory after checking live UUID references."""

import json
import shutil
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps.projects.models import Project
from core.apps.strategies.models import Strategy
from . import service
from .models import ResearchArtifact

_deleting: set[UUID] = set()
_guard_lock = Lock()


def _unsafe(reason: str) -> HTTPException:
    return HTTPException(409, {"code": "artifact_delete_unsafe", "reason": reason})


@contextmanager
def _deletion_guard(identifier: UUID, session: Session):
    with _guard_lock:
        if identifier in _deleting:
            raise HTTPException(409, {"code": "artifact_deletion_in_progress", "reason": "该成果正在删除"})
        _deleting.add(identifier)
    connection = None
    key = (identifier.int & 0xFFFFFFFF) - (1 << 31)
    acquired = False
    try:
        if session.get_bind().dialect.name == "postgresql":
            connection = session.get_bind().connect()
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(1936682082, :key)"), {"key": key})
            if not acquired:
                raise HTTPException(409, {"code": "artifact_deletion_in_progress", "reason": "该成果正在删除"})
        yield
    finally:
        try:
            if connection is not None:
                try:
                    if acquired:
                        connection.execute(text("SELECT pg_advisory_unlock(1936682082, :key)"), {"key": key})
                except Exception:
                    connection.invalidate()
                    raise
                finally:
                    connection.close()
        finally:
            with _guard_lock:
                _deleting.discard(identifier)


def _references(value, identifier: UUID) -> bool:
    if isinstance(value, dict):
        return any((key in {"id", "artifact_id", "artifactId"} and item == str(identifier))
                   or (key == str(identifier) and item is not None) or _references(item, identifier)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_references(item, identifier) for item in value)
    return value == str(identifier)


def require_unused(session: Session, artifact: ResearchArtifact) -> None:
    refs = []
    for project in session.scalars(select(Project)):
        if _references(project.artifact_dependencies, artifact.id):
            refs.append({"type": "project", "id": str(project.id), "name": project.name})
    for other in session.scalars(select(ResearchArtifact).where(ResearchArtifact.id != artifact.id)):
        if _references(other.dependencies, artifact.id):
            refs.append({"type": "artifact", "id": str(other.id), "package": other.package_name})
    for strategy in session.scalars(select(Strategy)):
        if _references(strategy.components, artifact.id) or _references(strategy.accepted_artifacts, artifact.id):
            refs.append({"type": "strategy", "id": str(strategy.id), "name": strategy.name})
    if refs:
        raise HTTPException(409, {"code": "artifact_in_use",
            "reason": "该成果仍被项目、策略或其他成果依赖闭包引用，不能删除", "refs": refs})


def _directory(path: Path, root: Path) -> bool:
    resolved = path.resolve()
    if resolved == root.resolve() or not resolved.is_relative_to(root.resolve()):
        raise _unsafe("成果清理目标越过自己的仓库边界")
    if path.is_symlink() or path.is_junction():
        raise _unsafe("成果清理目标不能替换为链接")
    if path.exists() and not path.is_dir():
        raise _unsafe("成果清理目标不是目录")
    return path.exists()


def _read_stage(stage: Path, root: Path, identifier: UUID) -> dict:
    if not _directory(stage, root):
        raise _unsafe("成果清理暂存目录不存在")
    contents = {path.name for path in stage.iterdir()}
    if contents - {"metadata.json", "artifact"}:
        raise _unsafe("成果清理暂存目录包含未知内容")
    if "metadata.json" not in contents:
        raise _unsafe("成果清理暂存目录缺少清理记录")
    marker_path = service.checked_path(stage / "metadata.json", stage)
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    if marker["artifact_id"] != str(identifier):
        raise _unsafe("成果清理记录不属于原 UUID")
    storage = marker["storage_directory"]
    if not isinstance(storage, str) or not storage or Path(storage).name != storage or storage in {".", ".."}:
        raise _unsafe("成果清理记录的存储目录无效")
    return marker


def restore_staged(stage: Path, root: Path, artifact: ResearchArtifact) -> None:
    marker = _read_stage(stage, root, artifact.id)
    if marker["storage_directory"] != artifact.storage_directory:
        raise _unsafe("成果清理记录不属于当前存储目录")
    payload = stage / "artifact"
    source = service.artifact_path(artifact).parent
    if _directory(payload, root):
        if source.exists():
            raise _unsafe("原成果目录已存在，不能覆盖恢复")
        payload.rename(source)
    elif not _directory(source, root):
        raise _unsafe("原成果和清理暂存均不存在")
    (stage / "metadata.json").unlink()
    stage.rmdir()


def purge_staged(stage: Path, root: Path, identifier: UUID) -> None:
    # Only the final empty directory may remain after its marker was removed.
    if stage == root / f".delete-{identifier}" and _directory(stage, root) and not any(stage.iterdir()):
        stage.rmdir()
        return
    _read_stage(stage, root, identifier)
    payload = stage / "artifact"
    if _directory(payload, root):
        shutil.rmtree(payload)
    (stage / "metadata.json").unlink()
    stage.rmdir()


def delete_artifact(identifier: UUID, session: Session) -> None:
    with _deletion_guard(identifier, session):
        service.lock_registry(session)
        artifact = session.scalar(select(ResearchArtifact).where(ResearchArtifact.id == identifier)
            .with_for_update().execution_options(populate_existing=True))
        root = Path(SoloSettings.SHARED_DIR).absolute() / "artifacts"
        if root.resolve() != (Path(SoloSettings.SHARED_DIR).resolve() / "artifacts"):
            raise _unsafe("成果仓库重定向至其他目录")
        stage = root / f".delete-{identifier}"
        if artifact is None:
            if not stage.exists():
                raise HTTPException(404, {"code": "artifact_not_found", "reason": "成果不存在或已经删除"})
            purge_staged(stage, root, identifier)
            session.rollback()
            return
        if artifact.published_at is None:
            raise HTTPException(422, {"code": "artifact_not_published", "reason": "仅允许删除已发布成果"})
        if stage.exists():
            restore_staged(stage, root, artifact)
        require_unused(session, artifact)
        source = service.artifact_path(artifact).parent
        staged = _directory(source, root)
        if staged:
            stage.mkdir()
            with (stage / "metadata.json").open("x", encoding="utf-8") as file:
                json.dump({"artifact_id": str(identifier), "storage_directory": artifact.storage_directory}, file)
            source.rename(stage / "artifact")
        try:
            session.delete(artifact)
            session.commit()
        except Exception:
            session.rollback()
            # A lost commit reply must not restore files for an already-deleted row.
            persisted = session.get(ResearchArtifact, identifier)
            if persisted is not None and staged:
                restore_staged(stage, root, persisted)
            raise
        if staged:
            try:
                purge_staged(stage, root, identifier)
            except OSError as error:
                raise HTTPException(503, {"code": "artifact_cleanup_incomplete",
                    "reason": "成果已删除，文件清理未完成，请按原成果 ID 重试", "deleted": True, "id": str(identifier)}) from error
