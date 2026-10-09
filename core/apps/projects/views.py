import json
import shutil
import tempfile
import tomllib
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.database.session import get_database_session

from . import templates
from .deletion import delete_project, project_lifecycle_guard
from .environment import EnvironmentError, kernel_directory, prepare_environment
from .models import Project
from .schemas import (
    ProjectCreate,
    ProjectEdit,
    ProjectKind,
    ProjectRead,
    TemplateVersion,
)

router = APIRouter(prefix="/api/v1", tags=["projects"])
type Database = Annotated[Session, Depends(get_database_session)]


def project_directory(project: Project) -> Path:
    return SoloSettings.SHARED_DIR / "projects" / project.kind / project.name


def write_project_metadata(project: Project, directory: Path) -> None:
    (directory / ".solo").write_text(json.dumps({
        "project_id": str(project.id), "name": project.name, "kind": project.kind,
        "scheme_version": project.scheme_version, "scheme_commit": project.scheme_commit,
        "algo_version": project.template_tag, "algo_commit": project.template_commit,
        "template_package_version": project.template_package_version,
        "package_name": project.package_name,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def read_project(project: Project) -> ProjectRead:
    from .versions import read_version

    reason = policy.project_retirement(project)
    return ProjectRead(
        id=project.id, name=project.name, description=project.description, kind=project.kind,
        packageName=project.package_name, artifactDependencies=project.artifact_dependencies or {},
        schemeVersion=project.scheme_version, schemeCommit=project.scheme_commit,
        algoVersion=project.template_tag, algoCommit=project.template_commit,
        createdAt=project.created_at, updatedAt=project.updated_at, archived=project.archived,
        directory=f"projects/{project.kind}/{project.name}",
        retired=bool(reason), retiredReason=reason,
        versions=[read_version(version) for version in project.versions],
    )


def get_project_or_404(session: Session, project_id: UUID, *, populate_existing: bool = False) -> Project:
    project = (session.get(Project, project_id, populate_existing=True) if populate_existing
               else session.get(Project, project_id))
    if project is None or project.archived:
        raise HTTPException(404, "项目不存在")
    return project


def require_not_deleting(session: Session, project_id: UUID) -> None:
    from .models import ProjectDeletion

    if session.get(ProjectDeletion, project_id, populate_existing=True) is not None:
        raise HTTPException(409, "项目正在删除或等待资源清理，请按原项目 ID 重试删除")


@router.get("/templates/scheme-versions", response_model=list[TemplateVersion])
def scheme_versions(refresh: bool = False) -> list[TemplateVersion]:
    try:
        return templates.list_scheme_versions(refresh)
    except templates.TemplateError as error:
        raise HTTPException(502, str(error)) from error


@router.get("/templates/versions", response_model=list[TemplateVersion])
def template_versions(kind: ProjectKind, scheme_version: str, refresh: bool = False) -> list[TemplateVersion]:
    try:
        return templates.list_versions(kind, scheme_version, refresh)
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except templates.TemplateError as error:
        raise HTTPException(502, str(error)) from error


@router.get("/projects", response_model=list[ProjectRead])
def list_projects(session: Database) -> list[ProjectRead]:
    from .versions import refresh_version

    projects = list(session.scalars(select(Project).where(Project.archived.is_(False)).order_by(Project.updated_at.desc(), Project.id)))
    for project in projects:
        for version in project.versions:
            refresh_version(version, workflow_kind=project.kind)
    session.commit()
    return [read_project(project) for project in projects]


@router.get("/projects/{project_id}", response_model=ProjectRead)
def get_project(project_id: UUID, session: Database) -> ProjectRead:
    return read_project(get_project_or_404(session, project_id))


@router.post("/projects", response_model=ProjectRead, status_code=201)
def create_project(body: ProjectCreate, session: Database) -> ProjectRead:
    try:
        for kind, tag in (("scheme", body.scheme_version), ("template", body.algo_version)):
            retired = policy.retired_release(kind, tag=tag)
            if retired:
                raise policy.VersionPolicyError(retired["reason"])
        scheme = templates.select_scheme(body.scheme_version)
        actual = scheme.version or templates.project_metadata(SoloSettings.SCHEME_REPOSITORY, scheme.commit, "pyproject.toml")["version"]
        policy.require_release("scheme", tag=scheme.tag, commit=scheme.commit, version=actual)
        version = next((v for v in templates.list_versions(body.kind, body.scheme_version) if v.tag == body.algo_version), None)
        if version is None:
            raise HTTPException(422, "Algo 版本不存在或不兼容所选 scheme，请刷新版本列表")
        template_actual = version.version or templates.project_metadata(
            SoloSettings.TEMPLATE_REPOSITORY, version.commit, f"{body.kind}/pyproject.toml",
        )["version"]
        policy.require_release("template", tag=version.tag, commit=version.commit, version=template_actual)
        from core.apps.artifacts.models import ResearchArtifact
        from .models import ProjectDeletion

        if session.scalar(select(Project.id).where(Project.kind == body.kind, Project.name == body.name)):
            raise HTTPException(409, "该类型下已有同名项目")
        used_packages = set(session.scalars(select(Project.package_name)))
        used_packages.update(session.scalars(select(ResearchArtifact.package_name)))
        if sum(f"{body.kind}-{suffix:04x}" in used_packages for suffix in range(16 ** 4)) == 16 ** 4:
            raise HTTPException(409, "该项目类型的 4 位包名已用完")
        for _ in range(16 ** 4):
            project_id = uuid4()
            package_name = f"{body.kind}-{project_id.hex[:4]}"
            if (package_name not in used_packages and session.get(Project, project_id) is None
                    and session.get(ProjectDeletion, project_id) is None):
                break
        else:
            raise HTTPException(503, "无法分配新的项目包名，请稍后重试")
        project = Project(id=project_id, name=body.name, description=body.description, kind=body.kind,
                          package_name=package_name,
                          template_tag=version.tag, template_commit=version.commit,
                          template_package_version=template_actual,
                          scheme_version=scheme.tag, scheme_commit=scheme.commit)
        destination = project_directory(project)
        if destination.exists() or destination.is_symlink() or destination.is_junction():
            raise HTTPException(409, "同名目录已存在（可能是删除时保留的工作区），请移走该目录或使用其他项目名称")
        destination.mkdir(parents=True, exist_ok=False)
        try:
            templates.create_directory(body.kind, version.commit, destination, project.id.hex[:4])
            unpacked = tomllib.loads((destination / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
            if unpacked != template_actual:
                raise ValueError("模板归档实际版本与所选 commit 的 metadata 不一致")
            templates.pin_scheme(destination, scheme)
            write_project_metadata(project, destination)
            prepare_environment(destination, str(project.id), project.name)
            session.add(project)
            session.commit()
        except Exception:
            session.rollback()
            shutil.rmtree(destination)
            kernel = kernel_directory(str(project.id))
            if kernel.exists():
                shutil.rmtree(kernel)
            raise
        return read_project(project)
    except templates.TemplateError as error:
        raise HTTPException(502, str(error)) from error
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    except EnvironmentError as error:
        raise HTTPException(502, str(error)) from error
    except (FileExistsError, IntegrityError) as error:
        raise HTTPException(409, "该类型下的项目名称已被使用") from error
    except OSError as error:
        raise HTTPException(503, "无法创建项目目录，请检查共享目录权限") from error


@router.patch("/projects/{project_id}", response_model=ProjectRead)
def edit_project(project_id: UUID, body: ProjectEdit, session: Database) -> ProjectRead:
    # Acquire before journal/project reads, and retain through rollback and backup cleanup.
    with project_lifecycle_guard(project_id, session):
        try:
            return _edit_project(project_id, body, session)
        except Exception:
            session.rollback()
            raise


def _edit_project(project_id: UUID, body: ProjectEdit, session: Session) -> ProjectRead:
    require_not_deleting(session, project_id)
    project = get_project_or_404(session, project_id, populate_existing=True)
    if project.name == body.name:
        project.description = body.description
        session.commit()
        return read_project(project)
    from core.apps.artifacts.service import ArtifactError, require_settled_installations

    try:
        # Do not invalidate an accepted install's exact workspace recovery bytes/paths.
        require_settled_installations(session, project_id)
    except ArtifactError as error:
        raise HTTPException(409, error.detail) from error
    source = project_directory(project)
    try:
        original_lock = (source / "uv.lock").read_bytes()
        original_scheme = policy.require_project(project, lock=tomllib.loads(original_lock.decode("utf-8")))
        original_pyproject = (source / "pyproject.toml").read_bytes()
        scheme_source = tomllib.loads(original_pyproject.decode("utf-8")).get("tool", {}).get("uv", {}).get("sources", {}).get("scheme", {})
        retired = policy.retired_release("scheme", tag=scheme_source.get("tag"), commit=scheme_source.get("rev"))
        if retired:
            raise policy.VersionPolicyError(retired["reason"])
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, {"code": "invalid_version_source", "reason": "项目环境尚未记录有效的冻结来源"}) from error
    destination = source.with_name(body.name)
    if destination.exists() or session.scalar(select(Project.id).where(Project.kind == project.kind, Project.name == body.name)):
        raise HTTPException(409, "该类型下已有同名项目或目录")
    if not source.is_dir():
        raise HTTPException(409, "项目目录不存在，请检查共享卷挂载")
    # 虚拟环境含绝对路径；重命名时重建环境，并保留原环境供失败回滚。
    kernel_file = kernel_directory(str(project.id)) / "kernel.json"
    old_kernel = kernel_file.read_bytes()
    original_files = {"pyproject.toml": original_pyproject, "uv.lock": original_lock,
                      ".solo": (source / ".solo").read_bytes(),
                      "research.ipynb": (source / "research.ipynb").read_bytes()}
    backup = Path(tempfile.mkdtemp(prefix=".rename-", dir=source.parent))
    try:
        source.rename(destination)
        (destination / ".venv").rename(backup / ".venv")
        project.name, project.description = body.name, body.description
        write_project_metadata(project, destination)
        prepare_environment(destination, str(project.id), project.name, locked=True)
        # Admission is repeated after installation, before any database commit.
        # --locked validates edited TOML against the original lock; it must never
        # resolve a newer/retired Scheme or replace any accepted package source.
        if policy.require_project(project) != original_scheme:
            raise ValueError("重命名不能改变实际 Scheme")
        if ((destination / "uv.lock").read_bytes() != original_lock
                or (destination / "pyproject.toml").read_bytes() != original_pyproject):
            raise ValueError("重命名不能改变冻结锁文件或依赖来源")
        session.commit()
    except Exception as error:
        session.rollback()
        if destination.exists():
            if (backup / ".venv").exists():
                shutil.rmtree(destination / ".venv", ignore_errors=True)
                (backup / ".venv").rename(destination / ".venv")
            destination.rename(source)
            for name, content in original_files.items():
                (source / name).write_bytes(content)
        kernel_file.write_bytes(old_kernel)
        if isinstance(error, policy.VersionPolicyError):
            raise HTTPException(422, error.detail) from error
        if isinstance(error, EnvironmentError):
            raise HTTPException(502, str(error)) from error
        raise HTTPException(503, "重命名项目失败，已恢复原目录和环境") from error
    finally:
        shutil.rmtree(backup)
    return read_project(project)


@router.delete("/projects/{project_id}", status_code=204)
def remove_project(
    project_id: UUID, session: Database,
    delete_files: Annotated[bool, Query(description="删除项目工作区；false 仅保留工作区文件夹，任务与 Kernel 注册仍删除")] = True,
) -> Response:
    delete_project(project_id, session, delete_files=delete_files)
    return Response(status_code=204)


@router.get("/projects/{project_id}/jupyter")
def open_jupyter(project_id: UUID, session: Database) -> RedirectResponse:
    require_not_deleting(session, project_id)
    project = get_project_or_404(session, project_id)
    if not project_directory(project).is_dir():
        raise HTTPException(409, "项目目录不存在，请检查共享卷挂载")
    path = quote(f"projects/{project.kind}/{project.name}/research.ipynb", safe="/")
    url = f"{SoloSettings.JUPYTER_URL}/lab/tree/{path}"
    if SoloSettings.JUPYTER_TOKEN:
        url += "?" + urlencode({"token": SoloSettings.JUPYTER_TOKEN})
    return RedirectResponse(url, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
