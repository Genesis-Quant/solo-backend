"""Published-only hard deletion; the registry row and its own digest directory.

Accepted tasks own their immutable run-local copies. They are not registry leases.
The small same-mount filesystem journal survives a lost commit acknowledgement or
partial cleanup without introducing a database tombstone or touching source runs.
"""

import hashlib
import json
import os
import re
import shutil
import stat
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from threading import Lock
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps.projects.models import Project, ProjectVersion, utc_now
from core.apps.strategies.models import Strategy
from . import service
from .models import ArtifactInstallation, ResearchArtifact

_DIGEST = re.compile(r"[0-9a-f]{64}")
_MARKER = "metadata.json"
_PAYLOAD = "artifact"
_deleting: set[UUID] = set()
_guard_lock = Lock()


def _unsafe(reason: str) -> HTTPException:
    return HTTPException(409, {"code": "artifact_delete_unsafe", "reason": reason})


def cleanup_incomplete(identifier: UUID) -> HTTPException:
    return HTTPException(503, {"code": "artifact_cleanup_incomplete",
        "reason": "成果已删除，但原成果文件清理未完成，请按原成果 ID 重试删除",
        "deleted": True, "id": str(identifier)})


@contextmanager
def _deletion_guard(identifier: UUID, session: Session):
    # The transaction registry lock ends at commit. This UUID-specific guard
    # also serializes post-commit purge/retries, including separate API workers.
    with _guard_lock:
        if identifier in _deleting:
            raise HTTPException(409, {"code": "artifact_deletion_in_progress",
                                      "reason": "该成果正在删除，请稍后重试"})
        _deleting.add(identifier)
    connection = None
    acquired = False
    key = (identifier.int & 0xFFFFFFFF) - (1 << 31)
    try:
        if session.get_bind().dialect.name == "postgresql":
            connection = session.get_bind().connect()
            acquired = connection.scalar(text("SELECT pg_try_advisory_lock(1936682082, :key)"), {"key": key})
            if not acquired:
                raise HTTPException(409, {"code": "artifact_deletion_in_progress",
                                          "reason": "该成果正在删除，请稍后重试"})
        yield
    finally:
        try:
            if connection is not None:
                if acquired:
                    connection.execute(text("SELECT pg_advisory_unlock(1936682082, :key)"), {"key": key})
                connection.close()
        finally:
            with _guard_lock:
                _deleting.discard(identifier)


def _lstat(path: Path, *, allow_link=False):
    try:
        value = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(value.st_mode) and allow_link:
        return value  # Read/unlink the link itself only, never its target.
    if (stat.S_ISLNK(value.st_mode) or path.is_junction()
            or getattr(value, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
        raise _unsafe("成果清理路径包含符号链接或目录联接，未删除任何文件")
    return value


def _ancestors(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts:
        raise _unsafe("成果清理路径不是安全的绝对路径")
    # Check the entire ancestry, including the shared root and its parents.
    for parent in path.parents:
        value = _lstat(parent)
        if value is not None and not stat.S_ISDIR(value.st_mode):
            raise _unsafe("成果清理边界不是文件夹")
    _lstat(path)


def _registry_root() -> Path:
    root = Path(SoloSettings.SHARED_DIR).absolute() / "artifacts"
    _ancestors(root)
    value = _lstat(root)
    if value is not None and not stat.S_ISDIR(value.st_mode):
        raise _unsafe("成果仓库不是安全的文件夹")
    return root


def _directory(path: Path, root: Path) -> bool:
    if path == root or not path.is_relative_to(root):
        raise _unsafe("成果清理目标越过了自己的仓库边界")
    _ancestors(path)
    if not path.resolve().is_relative_to(root.resolve()):
        raise _unsafe("成果清理路径重定向到仓库外")
    value = _lstat(path)
    if value is not None and not stat.S_ISDIR(value.st_mode):
        raise _unsafe("成果清理目标不是文件夹")
    return value is not None


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree(directory: Path, root: Path) -> tuple[dict, list[str]]:
    """Inspect before traversing; never follow a symlink, junction, or special file."""
    files, directories = {}, []
    if not _directory(directory, root):
        return files, directories
    pending = [directory]
    while pending:
        parent = pending.pop()
        for path in parent.iterdir():
            value = _lstat(path, allow_link=True)
            if value is None:
                raise _unsafe("成果文件在清理检查期间发生变化，请重试")
            relative = path.relative_to(directory).as_posix()
            if stat.S_ISLNK(value.st_mode):
                files[relative] = {"link": os.readlink(path)}
            elif stat.S_ISDIR(value.st_mode):
                directories.append(relative)
                pending.append(path)
            elif stat.S_ISREG(value.st_mode):
                files[relative] = {"sha256": _hash_file(path), "size_bytes": value.st_size}
            else:
                raise _unsafe("成果目录包含非普通文件，不能安全删除")
    return files, sorted(directories)


def _same_id(value, identifier: UUID) -> bool:
    try:
        return UUID(str(value)) == identifier
    except (ValueError, TypeError, AttributeError):
        return False


def _references(value, artifact: ResearchArtifact) -> bool:
    """Exact object IDs/digests only; a shared package name is not a reference."""
    if isinstance(value, dict):
        if (any(_same_id(value.get(key), artifact.id) for key in ("id", "artifact_id", "artifactId"))
                or value.get("sha256") == artifact.sha256
                or value.get("hash") == "sha256:" + artifact.sha256):
            return True
        return any((_same_id(key, artifact.id) and item is not None) or _references(item, artifact)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_references(item, artifact) for item in value)
    return _same_id(value, artifact.id)


def _accepted(record) -> bool:
    return bool(record.accepted_artifacts and _DIGEST.fullmatch(record.input_sha256 or "")
                and _DIGEST.fullmatch(record.lock_sha256 or ""))


def require_unused(session: Session, artifact: ResearchArtifact) -> None:
    # All mutators take the registry lock first. Read live refs only afterwards;
    # no origin join, package-name matching, or historical snapshot rewrite.
    refs = []
    for identifier, name, snapshots in session.execute(select(Project.id, Project.name, Project.artifact_dependencies)):
        if _references(snapshots, artifact):
            refs.append({"type": "project", "id": str(identifier), "name": name})
    now = utc_now()
    live_consumers = select(Project.id).where(Project.archived.is_(False))
    for installation in session.scalars(select(ArtifactInstallation)
            .where(ArtifactInstallation.consumer_project_id.in_(live_consumers))
            .execution_options(populate_existing=True)):
        receipt = installation.artifacts or {}
        if service.installation_references_live(installation, now) and any(
                _references(receipt.get(key), artifact) for key in ("before", "pending", "accepted")):
            refs.append({"type": "installation", "id": str(installation.id),
                         "project_id": str(installation.consumer_project_id)})
    for identifier, package, dependencies, published_at in session.execute(select(
            ResearchArtifact.id, ResearchArtifact.package_name, ResearchArtifact.dependencies, ResearchArtifact.published_at)
            .where(ResearchArtifact.id != artifact.id)):
        if _references(dependencies, artifact):
            refs.append({"type": "artifact", "id": str(identifier), "package": package,
                         "published": published_at is not None})
    for strategy in session.scalars(select(Strategy).where(Strategy.status.in_(("building", "queued", "running")))
                                   .execution_options(populate_existing=True)):
        if not _accepted(strategy) and (_references(strategy.components, artifact)
                                       or _references(strategy.accepted_artifacts, artifact)):
            refs.append({"type": "strategy_build", "id": str(strategy.id), "name": strategy.name})
    for version in session.scalars(select(ProjectVersion).where(ProjectVersion.status.in_(("building", "queued", "running")))
                                  .execution_options(populate_existing=True)):
        if not _accepted(version) and _references(version.accepted_artifacts, artifact):
            refs.append({"type": "version_build", "id": str(version.id), "project_id": str(version.project_id)})
    if refs:
        reasons = {"project": "当前项目", "installation": "尚未结束的依赖安装",
                   "strategy_build": "尚未接受的策略构建", "version_build": "尚未接受的研究构建"}
        labels = []
        for item in refs[:5]:
            kind = ("已发布成果依赖闭包" if item["published"] else "未发布成果依赖闭包") if item["type"] == "artifact" else reasons[item["type"]]
            name = " ".join(str(item.get("name") or item.get("package") or "").split())[:60]
            label = f"{kind} {name}（{item['id']}）"
            if item["type"] == "installation":
                label += f"，项目 {item['project_id']}"
            labels.append(label)
        more = f"等 {len(refs)} 处引用" if len(refs) > 5 else ""
        raise HTTPException(409, {"code": "artifact_in_use",
            "reason": f"该成果仍被{'；'.join(labels)}{more}引用，不能删除；请先解除实际依赖或等待操作结束",
            "refs": refs[:100]})


def _stage_path(root: Path, identifier: UUID, digest: str) -> Path:
    if not _DIGEST.fullmatch(digest):
        raise _unsafe("成果 SHA 身份无效，不能删除")
    return root / f".delete-{identifier}-{digest}"


def _own_stage(root: Path, identifier: UUID) -> Path | None:
    if _lstat(root) is None:
        return None
    prefix = f".delete-{identifier}-"
    found = []
    # Inspect direct children only, not a wildcard/rglob that could traverse links.
    for path in root.iterdir():
        if path.name.startswith(prefix):
            if not _DIGEST.fullmatch(path.name[len(prefix):]):
                raise _unsafe("原成果的清理暂存名称无效，不能继续删除")
            found.append(path)
    if len(found) > 1:
        raise _unsafe("原成果存在冲突的清理暂存目录，不能继续删除")
    return found[0] if found else None


def _relative(value) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        return False
    path = PurePosixPath(value)
    return bool(path.parts) and not path.is_absolute() and path.as_posix() == value and all(part not in {".", ".."} for part in path.parts)


def _read_stage(stage: Path, root: Path, identifier: UUID, *, artifact=None, partial=False) -> dict:
    if not _directory(stage, root):
        raise _unsafe("原成果的清理暂存目录已丢失")
    if {path.name for path in stage.iterdir()} - {_MARKER, _PAYLOAD}:
        raise _unsafe("成果清理暂存目录包含未知内容，不能继续删除")
    path = stage / _MARKER
    value = _lstat(path)
    if value is None or not stat.S_ISREG(value.st_mode) or value.st_size > 16 * 1024 * 1024:
        raise _unsafe("成果清理所有权记录缺失或无效")
    try:
        marker = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeError, ValueError) as error:
        raise _unsafe("成果清理所有权记录无法读取") from error
    snapshot = marker.get("snapshot") if isinstance(marker, dict) else None
    digest = marker.get("sha256") if isinstance(marker, dict) else None
    files = marker.get("files") if isinstance(marker, dict) else None
    directories = marker.get("directories") if isinstance(marker, dict) else None
    if (not isinstance(marker, dict) or marker.get("format") != 1 or marker.get("artifact_id") != str(identifier)
            or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
            or stage != _stage_path(root, identifier, digest)
            or not isinstance(snapshot, dict) or snapshot.get("id") != str(identifier)
            or snapshot.get("artifact_id") != str(identifier) or snapshot.get("sha256") != digest
            or not isinstance(files, dict) or not isinstance(directories, list)
            or any(not _relative(name) for name in [*files, *directories])
            or len(directories) != len(set(directories)) or set(files) & set(directories)
            or not isinstance(marker.get("bundle_manifest_sha256"), str)
            or not _DIGEST.fullmatch(marker["bundle_manifest_sha256"])):
        raise _unsafe("成果清理记录与原始 UUID、SHA 或目录不一致")
    filename = snapshot.get("filename")
    if (not isinstance(filename, str) or not _relative(filename) or "/" in filename
            or not filename.endswith(".whl") or files.get(filename) != {
                "sha256": digest, "size_bytes": snapshot.get("size_bytes")}
            or files.get("bundle/manifest.json", {}).get("sha256") != marker["bundle_manifest_sha256"]):
        raise _unsafe("成果清理记录没有绑定原始 wheel 和发布清单")
    for entry in files.values():
        if isinstance(entry, dict) and set(entry) == {"link"} and isinstance(entry["link"], str):
            continue
        if (not isinstance(entry, dict) or set(entry) != {"sha256", "size_bytes"}
                or not isinstance(entry["sha256"], str) or not _DIGEST.fullmatch(entry["sha256"])
                or type(entry["size_bytes"]) is not int or entry["size_bytes"] < 0):
            raise _unsafe("成果清理文件清单无效")
    if artifact is not None and (snapshot != service.artifact_snapshot(artifact)
            or digest != artifact.sha256 or marker["bundle_manifest_sha256"] != artifact.bundle_manifest_sha256):
        raise _unsafe("成果清理暂存不属于当前原始成果记录")
    payload = stage / _PAYLOAD
    current_files, current_directories = _tree(payload, root)
    if ((not partial and (current_files != files or current_directories != sorted(directories)))
            or any(files.get(name) != value for name, value in current_files.items())
            or set(current_directories) - set(directories)):
        raise _unsafe("成果清理暂存内容被修改，不能恢复或删除")
    manifest_path = payload / "bundle" / "manifest.json"
    if "bundle/manifest.json" in current_files:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (UnicodeError, ValueError) as error:
            raise _unsafe("成果清理暂存发布清单无效") from error
        if (not isinstance(manifest, dict) or manifest.get("artifact_id") != str(identifier)
                or manifest.get("root_artifact") != snapshot):
            raise _unsafe("成果清理暂存发布清单属于其他成果")
    return marker


def restore_staged(stage: Path, root: Path, artifact: ResearchArtifact) -> None:
    payload = stage / _PAYLOAD
    marker = _read_stage(stage, root, artifact.id, artifact=artifact, partial=not _directory(payload, root))
    source = root / artifact.sha256
    if not _directory(payload, root):
        # Crash after durable marker but before rename (or after restore). Only
        # discard that marker when the full canonical tree still matches DB truth.
        files, directories = _tree(source, root)
        if files != marker["files"] or directories != sorted(marker["directories"]):
            raise _unsafe("成果清理暂存为空且原始文件不完整，不能继续删除")
    else:
        if _directory(source, root):
            raise _unsafe("原成果目录与暂存目录同时存在，不能覆盖恢复")
        payload.rename(source)
    (stage / _MARKER).unlink()
    stage.rmdir()


def stage_artifact(session: Session, root: Path, artifact: ResearchArtifact) -> Path | None:
    source = root / artifact.sha256
    if not _directory(source, root):
        return None  # Missing bytes are already gone; delete only the exact row.
    files, directories = _tree(source, root)
    # Verify ownership against immutable database truth, not an origin workspace.
    service.require_artifact(session, artifact.id)
    service.verify_bundle(artifact)
    stage = _stage_path(root, artifact.id, artifact.sha256)
    if _directory(stage, root):
        raise _unsafe("成果清理暂存路径已经存在，未覆盖任何文件")
    marker = {"format": 1, "artifact_id": str(artifact.id), "sha256": artifact.sha256,
        "snapshot": service.artifact_snapshot(artifact), "bundle_manifest_sha256": artifact.bundle_manifest_sha256,
        "files": files, "directories": directories}
    stage.mkdir(mode=0o700)
    try:
        # Ownership marker remains outside the payload through a partial rmtree.
        with (stage / _MARKER).open("x", encoding="utf-8") as file:
            json.dump(marker, file, ensure_ascii=False, sort_keys=True)
            file.flush()
            os.fsync(file.fileno())
        source.rename(stage / _PAYLOAD)
    except Exception:
        if not (stage / _PAYLOAD).exists():
            (stage / _MARKER).unlink(missing_ok=True)
            stage.rmdir()
        raise
    return stage


def purge_staged(stage: Path, root: Path, identifier: UUID) -> None:
    marker = _read_stage(stage, root, identifier, partial=True)
    payload = stage / _PAYLOAD
    if _directory(payload, root):
        # Normal uv environments contain Python symlinks. Remove those entries
        # themselves before recursive cleanup; never traverse their targets.
        for relative, entry in marker["files"].items():
            if "link" in entry:
                path = payload / relative
                _ancestors(path.parent)
                value = _lstat(path, allow_link=True)
                if value is not None:
                    if not stat.S_ISLNK(value.st_mode) or os.readlink(path) != entry["link"]:
                        raise _unsafe("成果清理暂存链接在检查后发生变化")
                    path.unlink()
        _tree(payload, root)  # Reject any newly inserted link/junction boundary.
        shutil.rmtree(payload)
    # Keep a valid marker if final directory removal fails, enabling a retry.
    content = (stage / _MARKER).read_bytes()
    (stage / _MARKER).unlink()
    try:
        stage.rmdir()
    except OSError:
        with (stage / _MARKER).open("xb") as file:
            file.write(content)
        raise


def delete_artifact(identifier: UUID, session: Session) -> None:
    """Own the commit/rollback; never restore files before reading persisted truth."""
    with _deletion_guard(identifier, session):
        stage = None
        root = None
        commit_started = False
        deleted = False
        artifact = None
        try:
            service.lock_registry(session)
            artifact = session.scalar(select(ResearchArtifact).where(ResearchArtifact.id == identifier)
                .with_for_update().execution_options(populate_existing=True))
            root = _registry_root()
            stage = _own_stage(root, identifier)
            if stage is not None and _directory(stage, root) and not any(stage.iterdir()):
                # A crash can happen immediately after mkdir, or between final
                # marker unlink and rmdir. Remove only a truly empty UUID stage;
                # a nonempty stage without a valid marker is never adopted.
                if artifact is not None and stage != _stage_path(root, identifier, artifact.sha256):
                    raise _unsafe("空清理暂存目录与原成果 SHA 不一致")
                stage.rmdir()
                if artifact is None:
                    session.rollback()
                    return
                stage = None
            if artifact is None:
                if stage is None:
                    raise HTTPException(404, {"code": "artifact_not_found", "reason": "成果不存在或已经删除"})
                deleted = True
                purge_staged(stage, root, identifier)
                session.rollback()  # Release the registry lock; no database changes.
                return
            if artifact.published_at is None:
                raise HTTPException(422, {"code": "artifact_not_published", "reason": "仅允许从发布成果面板删除已发布成果"})
            if not _DIGEST.fullmatch(artifact.sha256):
                raise _unsafe("成果 SHA 身份无效，未删除任何文件")
            if stage is not None:
                # A previous process stopped before commit: the original row is
                # still authoritative. Restore first, then recheck current refs.
                try:
                    restore_staged(stage, root, artifact)
                except HTTPException:
                    # Ownership/content validation fails before any restore
                    # mutation. Do not manufacture a second rollback attempt.
                    stage = None
                    raise
                stage = None
            require_unused(session, artifact)
            stage = stage_artifact(session, root, artifact)
            session.delete(artifact)
            commit_started = True
            session.commit()
            deleted = True
            if stage is not None:
                purge_staged(stage, root, identifier)
        except Exception as error:
            try:
                session.rollback()
                if commit_started and not deleted:
                    # Read database columns, not a cached/deleted ORM object.
                    service.lock_registry(session)
                    persisted = session.execute(select(ResearchArtifact.id, ResearchArtifact.sha256)
                        .where(ResearchArtifact.id == identifier).with_for_update()).one_or_none()
                    if persisted is None:
                        deleted = True
                    elif artifact is None or persisted.sha256 != artifact.sha256:
                        raise RuntimeError("Artifact deletion identity changed")
            except Exception as truth_error:
                session.rollback()
                raise HTTPException(503, {"code": "artifact_delete_state_unknown",
                    "reason": "无法确认成果删除提交状态；文件仍在原 UUID 暂存目录，请按原 ID 重试",
                    "id": str(identifier)}) from truth_error
            if deleted:
                session.rollback()
                raise cleanup_incomplete(identifier) from error
            if stage is not None and root is not None and artifact is not None:
                try:
                    service.lock_registry(session)
                    restore_staged(stage, root, artifact)
                except Exception as restore_error:
                    session.rollback()
                    raise HTTPException(503, {"code": "artifact_restore_incomplete",
                        "reason": "成果记录仍保留，但文件回滚未完成，请按原成果 ID 重试删除",
                        "deleted": False, "id": str(identifier)}) from restore_error
            session.rollback()
            if isinstance(error, HTTPException):
                raise
            if isinstance(error, service.ArtifactError):
                raise _unsafe("成果文件或发布清单无法验证，未删除成果") from error
            raise HTTPException(503, {"code": "artifact_delete_failed",
                "reason": "成果删除失败，记录和文件已保留，请稍后重试", "id": str(identifier)}) from error
