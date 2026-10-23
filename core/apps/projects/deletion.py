"""Hard deletion with a transient UUID journal and same-mount filesystem staging."""

import json
import shutil
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from threading import Lock
from urllib.parse import quote
from uuid import UUID, uuid4

import requests
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from config import DolphinSchedulerSettings, SoloSettings
from core.scheduler.client import DolphinSchedulerBusyError, DolphinSchedulerClient, DolphinSchedulerError

from .environment import kernel_directory
from .models import Project, ProjectVersion
from .schemas import ProjectEdit


ACTIVE_PHASES = ("building", "queued", "running")
_lifecycle_projects: set[UUID] = set()
_lifecycle_lock = Lock()


def project_lifecycle_busy(project_id: UUID) -> bool:
    with _lifecycle_lock:
        return project_id in _lifecycle_projects


@contextmanager
def project_lifecycle_guard(project_id: UUID, session: Session):
    """Exclude project edits, upgrades and deletion through filesystem rollback."""
    with _lifecycle_lock:
        if project_id in _lifecycle_projects:
            raise HTTPException(409, "项目正在修改或删除，请稍后重试")
        _lifecycle_projects.add(project_id)
    connection = None
    acquired = False
    # Keep the existing DELETE key so other workers use the same exclusion.
    key = (project_id.int & ((1 << 63) - 1))
    try:
        bind = session.get_bind()
        if bind.dialect.name == "postgresql":
            # A dedicated session lock survives application commits and rollbacks.
            connection = bind.connect()
            try:
                acquired = connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            except Exception:
                connection.invalidate()  # An acquire response can be lost after the lock is granted.
                raise
            if not acquired:
                raise HTTPException(409, "项目正在修改或删除，请稍后重试")
        yield
    finally:
        try:
            if connection is not None:
                try:
                    if acquired:
                        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                except Exception:
                    connection.invalidate()  # Never return a possibly locked connection to the pool.
                    raise
                finally:
                    connection.close()
        finally:
            with _lifecycle_lock:
                _lifecycle_projects.discard(project_id)


def checked_directory(directory: Path, boundary: Path) -> bool:
    resolved, boundary = directory.resolve(), boundary.resolve()
    if resolved == boundary or not resolved.is_relative_to(boundary):
        raise HTTPException(409, "项目清理路径越界，未删除任何文件")
    return directory.is_dir()


def read_metadata(path: Path) -> dict:
    if path.is_symlink() or path.is_junction() or not path.is_file():
        raise HTTPException(409, "无法确认文件夹所属项目，未删除任何文件")
    try:
        metadata = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise HTTPException(409, "无法确认文件夹所属项目，未删除任何文件") from error
    if not isinstance(metadata, dict):
        raise HTTPException(409, "无法确认文件夹所属项目，未删除任何文件")
    return metadata


def identity(project_id: UUID, name: str, kind: str) -> dict[str, str]:
    return {"project_id": str(project_id), "name": name, "kind": kind}


def require_identity(metadata: dict, expected: dict) -> None:
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise HTTPException(409, "文件夹所属项目与记录不一致，未删除任何文件")


def jupyter_request(path: str, headers: dict[str, str], *, params: dict | None = None,
                    method: str = "GET", body: dict | None = None) -> requests.Response:
    with requests.Session() as client:
        client.trust_env = False
        return client.request(method, SoloSettings.JUPYTER_API_URL + path, headers=headers,
                              params=params, json=body, timeout=(3, 10), allow_redirects=False)


def jupyter_headers() -> dict[str, str]:
    return {"Authorization": f"token {SoloSettings.JUPYTER_TOKEN}"} if SoloSettings.JUPYTER_TOKEN else {}


def journal_lease(job, session: Session) -> str:
    lease = job.payload.get("lease")
    if lease is None:
        lease = str(uuid4())
    # Existing journals may contain an older Jupyter-issued opaque token. Never re-key it.
    if not isinstance(lease, str) or not lease.strip() or len(lease) > 128:
        raise HTTPException(409, "项目清理锁记录无效，不能继续删除")
    job.payload = {**job.payload, "lease": lease}
    session.commit()  # Commit must succeed before POST; a lost response still has a durable nonce.
    return lease


def acquire_lease(project_id: UUID, lease: str) -> None:
    try:
        response = jupyter_request("/solo/project/operations", jupyter_headers(), method="POST",
                                   body={"project_id": str(project_id), "operation": "delete", "lease": lease})
        if response.status_code == 409:
            raise HTTPException(409, "项目正在安装依赖、读取源码或保存版本，请稍后再删除")
        data = response.json() if response.status_code == 200 else None
        if (not isinstance(data, dict) or data.get("project_id") != str(project_id)
                or data.get("lease") != lease):
            raise ValueError
    except (requests.RequestException, ValueError, TypeError) as error:
        raise HTTPException(503, "无法取得 Jupyter 项目清理锁，请稍后重试") from error


def release_lease(project_id: UUID, lease: str) -> None:
    try:
        response = jupyter_request("/solo/project/operations", jupyter_headers(), method="DELETE",
                                   body={"project_id": str(project_id), "lease": lease})
        if response.status_code != 204:
            raise ValueError
    except (requests.RequestException, ValueError) as error:
        raise HTTPException(503, "Jupyter 项目清理锁释放未完成，请重试删除") from error


def jupyter_resources(project_id: UUID, directory: str, *, uuid_only: bool = False) -> tuple[list[str], list[str]]:
    """Match an exact subtree or solo-UUID; retries must never match a reused name."""
    try:
        payloads = []
        for resource in ("sessions", "kernels"):
            response = jupyter_request(f"/api/{resource}", jupyter_headers())
            data = response.json() if response.status_code == 200 else None
            if not isinstance(data, list):
                raise ValueError
            payloads.append(data)
        sessions, kernels = payloads
        by_id = {}
        for kernel in kernels:
            if (not isinstance(kernel, dict) or not isinstance(kernel.get("id"), str) or not kernel["id"]
                    or not isinstance(kernel.get("name"), str) or kernel["id"] in by_id):
                raise ValueError
            by_id[kernel["id"]] = kernel
        selected_sessions, selected_kernels = [], set()
        project_path = PurePosixPath(directory)
        kernel_name = f"solo-{project_id}"
        for item in sessions:
            if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(item.get("path"), str)):
                raise ValueError
            path = PurePosixPath(item["path"])
            if path.is_absolute() or ".." in path.parts or "\\" in item["path"]:
                raise ValueError
            kernel = item.get("kernel")
            if (not isinstance(kernel, dict) or not isinstance(kernel.get("id"), str)
                    or not isinstance(kernel.get("name"), str)):
                raise ValueError
            if kernel["name"] == kernel_name or (not uuid_only and path.is_relative_to(project_path)):
                selected_sessions.append(item["id"])
                selected_kernels.add(kernel["id"])
        selected_kernels.update(key for key, item in by_id.items() if item["name"] == kernel_name)
        for identifier in selected_kernels:
            kernel = by_id.get(identifier)
            if kernel is None or not isinstance(kernel.get("execution_state"), str):
                raise ValueError
            if kernel["execution_state"] != "idle":
                raise HTTPException(409, "项目 Kernel 正在执行任务，请结束后再删除")
            # A generic kernel shared with a foreign session is not exclusively owned.
            if any(item["kernel"]["id"] == identifier and item["id"] not in selected_sessions for item in sessions):
                raise HTTPException(409, "项目 Kernel 仍被其他项目使用，不能关闭")
        return selected_sessions, sorted(selected_kernels)
    except (requests.RequestException, ValueError, TypeError, KeyError) as error:
        raise HTTPException(503, "无法确认 Jupyter 使用状态，未删除项目，请稍后重试") from error


def stop_jupyter(project_id: UUID, directory: str, *, uuid_only: bool = False) -> None:
    sessions, kernels = jupyter_resources(project_id, directory, uuid_only=uuid_only)
    try:
        for resource, identifiers in (("sessions", sessions), ("kernels", kernels)):
            for identifier in identifiers:
                response = jupyter_request(f"/api/{resource}/{quote(identifier, safe='')}",
                                           jupyter_headers(), method="DELETE")
                if response.status_code not in {204, 404, 410}:
                    raise ValueError
        if any(jupyter_resources(project_id, directory, uuid_only=uuid_only)):
            raise HTTPException(409, "项目 Notebook 或 Kernel 尚未关闭，请稍后重试")
    except (requests.RequestException, ValueError) as error:
        raise HTTPException(503, "Jupyter 项目资源关闭未完成，请重试删除") from error


def require_inactive(project_id: UUID, session: Session) -> list[ProjectVersion]:
    versions = list(session.scalars(select(ProjectVersion).where(
        ProjectVersion.project_id == project_id).with_for_update()))
    if any(version.status in ACTIVE_PHASES for version in versions):
        raise HTTPException(409, "项目仍有构建中或运行中的研究任务，不能删除")
    return versions


def resource_plan(project_id: UUID, name: str, kind: str, delete_files: bool, run_ids: list[str]) -> list[dict]:
    try:
        if ProjectEdit(name=name).name != name or kind not in {"factor", "model", "optimize", "control", "execution"}:
            raise ValueError
        if any(str(UUID(value)) != value for value in run_ids) or len(run_ids) != len(set(run_ids)):
            raise ValueError
    except (ValueError, TypeError) as error:
        raise HTTPException(409, "项目清理身份或路径无效，未删除任何文件") from error
    shared = SoloSettings.SHARED_DIR
    workspace = shared / "projects" / kind / name
    sources = ([("project", workspace, shared)] if delete_files else [])
    sources += [(value, shared / "runs" / value, shared) for value in run_ids]
    kernel = kernel_directory(str(project_id))
    sources.append(("kernel", kernel, kernel.parent))
    return [{"source": str(source), "staged": str(source.parent / f".delete-{project_id}" / label),
             "boundary": str(boundary)} for label, source, boundary in sources]


def validate_plan(job) -> list[dict]:
    payload = job.payload
    if not isinstance(payload, dict) or payload.get("format") != 1 or not isinstance(payload.get("run_ids"), list):
        raise HTTPException(409, "项目清理记录不完整，不能继续删除")
    expected = resource_plan(job.id, job.project_name, job.kind, job.delete_files, payload["run_ids"])
    if payload.get("paths") != expected:
        raise HTTPException(409, "项目清理路径与原始身份不一致，不能继续删除")
    return expected


def stage_containers(paths: list[dict]) -> dict[Path, Path]:
    return {Path(item["staged"]).parent: Path(item["boundary"]) for item in paths}


def validate_container(container: Path, boundary: Path, expected: dict, allowed: set[str]) -> bool:
    if not checked_directory(container, boundary):
        return False
    children = list(container.iterdir())
    if children:
        require_identity(read_metadata(container / "metadata.json"), expected)
        if any(path.name not in allowed | {"metadata.json"} for path in children):
            raise HTTPException(409, "项目清理暂存目录包含未知文件，不能继续删除")
    return True


def restore_staged(paths: list[dict], expected: dict) -> None:
    for container, boundary in stage_containers(paths).items():
        allowed = {Path(item["staged"]).name for item in paths if Path(item["staged"]).parent == container}
        if not validate_container(container, boundary, expected, allowed):
            continue
        for item in reversed(paths):
            staged, source = Path(item["staged"]), Path(item["source"])
            if staged.parent != container or not checked_directory(staged, boundary):
                continue
            if checked_directory(source, boundary):
                raise HTTPException(409, "原项目目录与清理暂存目录冲突，不能恢复")
            staged.rename(source)
        (container / "metadata.json").unlink(missing_ok=True)
        container.rmdir()


def validate_sources(project: Project, paths: list[dict]) -> None:
    workspace = SoloSettings.SHARED_DIR / "projects" / project.kind / project.name
    expected = identity(project.id, project.name, project.kind)
    for container, boundary in stage_containers(paths).items():
        if checked_directory(container, boundary):
            raise HTTPException(409, "项目清理暂存目录已存在且没有对应清理记录，未删除任何文件")
    # Even a retained workspace must not redirect project ownership to another folder.
    if checked_directory(workspace, SoloSettings.SHARED_DIR):
        require_identity(read_metadata(workspace / ".solo"), expected)
    for item in paths:
        source = Path(item["source"])
        if checked_directory(source, Path(item["boundary"])) and source == kernel_directory(str(project.id)):
            specification = read_metadata(source / "kernel.json")
            argv = specification.get("argv")
            python = PurePosixPath(argv[0].replace("\\", "/")) if (
                isinstance(argv, list) and argv and isinstance(argv[0], str)) else None
            suffix = ("projects", project.kind, project.name, ".venv")
            if (python is None or not (python.is_absolute() or Path(argv[0]).is_absolute())
                    or python.parts[-6:-2] != suffix or python.parts[-2:] not in {
                        ("bin", "python"), ("Scripts", "python.exe")}):
                raise HTTPException(409, "Kernel 注册不属于该项目环境，未删除任何文件")
            metadata = specification.get("metadata", {})
            if not isinstance(metadata, dict):
                raise HTTPException(409, "Kernel 注册身份无效，未删除任何文件")
            owner = metadata.get("solo")
            if owner is not None and owner != {
                    "project_id": str(project.id), "path": f"projects/{project.kind}/{project.name}"}:
                raise HTTPException(409, "Kernel 注册身份与项目不一致，未删除任何文件")


def stage_sources(paths: list[dict], expected: dict, moved: list[dict]) -> None:
    for item in paths:
        source, staged, boundary = Path(item["source"]), Path(item["staged"]), Path(item["boundary"])
        if not checked_directory(source, boundary):
            continue
        container = staged.parent
        if not checked_directory(container, boundary):
            container.mkdir()
            (container / "metadata.json").write_text(json.dumps(expected, ensure_ascii=False), encoding="utf-8")
        if staged.exists() or staged.is_symlink() or staged.is_junction():
            raise HTTPException(409, "项目清理暂存路径已被占用，未继续删除")
        require_identity(read_metadata(container / "metadata.json"), expected)
        source.rename(staged)  # Kernel staging stays inside the mounted kernels directory.
        moved.append(item)


def purge_staged(paths: list[dict], expected: dict) -> None:
    for container, boundary in stage_containers(paths).items():
        allowed = {Path(item["staged"]).name for item in paths if Path(item["staged"]).parent == container}
        if not validate_container(container, boundary, expected, allowed):
            continue
        for item in paths:
            staged = Path(item["staged"])
            if staged.parent == container and checked_directory(staged, boundary):
                if staged.is_symlink():
                    staged.unlink()
                else:
                    shutil.rmtree(staged)
        # The outer ownership marker outlives partial rmtree, enabling safe retries.
        (container / "metadata.json").unlink(missing_ok=True)
        container.rmdir()


def cleanup_incomplete(project_id: UUID, reason: str) -> HTTPException:
    return HTTPException(503, {"code": "project_cleanup_incomplete", "reason": reason,
                               "deleted": True, "id": str(project_id)})


def delete_project(project_id: UUID, session: Session, *, delete_files: bool = True) -> None:
    with project_lifecycle_guard(project_id, session):
        _delete_project(project_id, session, delete_files=delete_files)


def _delete_project(project_id: UUID, session: Session, *, delete_files: bool) -> None:
    from .models import ProjectDeletion

    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update()
                             .execution_options(populate_existing=True))
    job = session.scalar(select(ProjectDeletion).where(ProjectDeletion.id == project_id).with_for_update()
                         .execution_options(populate_existing=True))
    if project is None and job is None:
        raise HTTPException(404, "项目不存在")
    deleted = project is None
    moved: list[dict] = []
    lease = None
    lease_rejected = False
    state_known = True
    staging_started = False
    restored = True
    try:
        if project is not None:
            versions = require_inactive(project_id, session)
            expected = identity(project.id, project.name, project.kind)
            if job is not None:
                require_identity(identity(job.id, job.project_name, job.kind), expected)
                recovery_paths = validate_plan(job)
                restored = False  # Do not drop a recovery journal before its staged paths are restored.
                lease = journal_lease(job, session)
                try:
                    acquire_lease(project_id, lease)
                except HTTPException as error:
                    lease_rejected = error.status_code == 409
                    raise
                restore_staged(recovery_paths, expected)
                restored = True
                delete_files = job.delete_files  # A retry cannot silently change the original request.
            from core.apps.strategies.models import Strategy
            if versions and session.scalar(select(Strategy.id).where(Strategy.id.in_([version.id for version in versions]))) is not None:
                raise HTTPException(409, "研究 Run 与独立策略 Run 身份冲突，未删除任何文件")
            run_ids = [str(version.id) for version in versions]
            paths = resource_plan(project.id, project.name, project.kind, delete_files, run_ids)
            validate_sources(project, paths)
            if job is None:
                job = ProjectDeletion(id=project_id, project_name=project.name, kind=project.kind,
                                      delete_files=delete_files, error="")
                session.add(job)
            job.payload = {"format": 1, "phase": "preparing", "run_ids": run_ids, "paths": paths,
                           "instances": [], "lease": lease}
            if lease is None:
                lease = journal_lease(job, session)
                try:
                    acquire_lease(project_id, lease)
                except HTTPException as error:
                    lease_rejected = error.status_code == 409
                    raise
            else:
                session.commit()
            project = session.scalar(select(Project).where(Project.id == project_id).with_for_update()
                                     .execution_options(populate_existing=True))
            if project is None:
                raise HTTPException(409, "项目记录已改变，请重试")
            require_identity(identity(project.id, project.name, project.kind), expected)
            versions = require_inactive(project_id, session)
            if {str(version.id) for version in versions} != set(run_ids):
                raise HTTPException(409, "项目研究任务已改变，请重试")
            known = {version.workflow_id: str(SoloSettings.SHARED_DIR / "runs" / str(version.id) / "input.json")
                     for version in versions if version.workflow_id}
            inputs = {str(SoloSettings.SHARED_DIR / "runs" / value / "input.json") for value in run_ids}
            if versions and DolphinSchedulerSettings.ENABLED:
                with DolphinSchedulerClient() as client:
                    instances = client.owned_instances(project.kind, inputs, known)
                job.payload = {**job.payload, "instances": instances}
                session.flush()
            elif known:
                raise HTTPException(503, "DolphinScheduler 不可用，无法确认任务清理，请稍后重试")
            stop_jupyter(project.id, f"projects/{project.kind}/{project.name}")
            validate_sources(project, paths)
            staging_started = True
            stage_sources(paths, expected, moved)
            if any(jupyter_resources(project.id, f"projects/{project.kind}/{project.name}")):
                raise HTTPException(409, "清理期间启动了项目 Kernel，已恢复项目，请稍后重试")
            job.payload = {**job.payload, "phase": "deleted"}
            session.delete(project)
            session.commit()  # Cascade removes versions; no permanent archive/tombstone remains.
            deleted = True
        else:
            paths = validate_plan(job)
            if job.payload.get("phase") != "deleted":
                raise HTTPException(409, "项目清理事务状态不完整，不能继续删除")
            lease = journal_lease(job, session)
            try:
                acquire_lease(project_id, lease)
            except HTTPException as error:
                lease_rejected = error.status_code == 409
                raise
            expected = identity(job.id, job.project_name, job.kind)
            stop_jupyter(job.id, "", uuid_only=True)
        if job.payload.get("instances"):
            with DolphinSchedulerClient() as client:
                for instance in list(job.payload["instances"]):
                    client.delete_owned_instance(instance)
                    job.payload = {**job.payload, "instances": [value for value in job.payload["instances"] if value != instance]}
                    session.commit()
        purge_staged(paths, expected)
        from core.apps.artifacts.service import gc_unreferenced
        gc_unreferenced(session)
        session.commit()
    except Exception as error:
        session.rollback()
        # A commit can succeed on the database before its acknowledgement is lost.
        # Never restore a deleted UUID's staged files into a newly reused workspace name.
        if lease is not None:
            try:
                saved_job = session.get(ProjectDeletion, project_id)
                project_exists = session.get(Project, project_id) is not None
                if not project_exists and saved_job is not None and saved_job.payload.get("phase") == "deleted":
                    job = saved_job
                    deleted = True
                elif not project_exists:
                    state_known = False
            except Exception:
                session.rollback()
                state_known = False
            if not state_known:
                raise HTTPException(503, "无法确认项目清理提交状态，请按原项目 ID 重试") from error
        if not deleted:
            try:
                if staging_started:
                    restore_staged(paths, expected)
            except (OSError, HTTPException):
                restored = False
                raise HTTPException(503, "项目清理回滚未完成，请按原项目 ID 重试") from error
        else:
            reason = error.detail if isinstance(error, HTTPException) and isinstance(error.detail, str) else "项目已删除，但资源清理未完成，请按原项目 ID 重试"
            try:
                job.error = reason
                session.commit()
            except Exception:
                session.rollback()  # The durable UUID intent still survives a progress-write failure.
            raise cleanup_incomplete(project_id, reason) from error
        if isinstance(error, DolphinSchedulerBusyError):
            raise HTTPException(409, "研究任务在 DolphinScheduler 中仍未结束，不能删除") from error
        if isinstance(error, (OSError, DolphinSchedulerError)):
            raise HTTPException(503, "项目清理失败，未删除项目，请稍后重试") from error
        raise
    finally:
        if lease is not None:
            try:
                # An explicit 409 never grants our token. Do not touch the real owner's gate.
                if not lease_rejected:
                    release_lease(project_id, lease)
                if state_known:
                    saved_job = session.get(ProjectDeletion, project_id)
                    if saved_job is not None:
                        if not deleted and restored:
                            session.delete(saved_job)
                        elif not lease_rejected:
                            saved_job.payload = {**saved_job.payload, "lease": None}
                        session.commit()
            except Exception as error:
                session.rollback()
                reason = error.detail if isinstance(error, HTTPException) else "项目清理锁或进度记录未确认完成，请按原项目 ID 重试"
                if deleted:
                    raise cleanup_incomplete(project_id, reason) from error
                raise HTTPException(503, reason) from error
    try:
        session.delete(job)
        session.commit()
    except Exception as error:
        session.rollback()
        raise cleanup_incomplete(project_id, "资源已清理，但清理记录删除未完成，请重试") from error
