from pathlib import Path
import json
import shutil
import tempfile
from typing import Annotated
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from config import SoloSettings
from core.database.session import get_database_session
from .models import Project
from .schemas import ProjectCreate, ProjectEdit, ProjectKind, ProjectRead, TemplateVersion
from . import templates
from .environment import EnvironmentError, kernel_directory, prepare_environment


router = APIRouter(prefix="/api/v1", tags=["projects"])
type Database = Annotated[Session, Depends(get_database_session)]


def project_directory(project: Project) -> Path:
    return SoloSettings.SHARED_DIR / "projects" / project.kind / project.name


def write_project_metadata(project: Project, directory: Path) -> None:
    (directory / ".solo").write_text(json.dumps({
        "project_id": str(project.id), "name": project.name, "kind": project.kind,
        "scheme_version": project.scheme_version, "scheme_commit": project.scheme_commit,
        "algo_version": project.template_tag, "algo_commit": project.template_commit,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


def read_project(project: Project) -> ProjectRead:
    from .versions import read_version

    return ProjectRead(
        id=project.id, name=project.name, description=project.description, kind=project.kind,
        schemeVersion=project.scheme_version, schemeCommit=project.scheme_commit,
        algoVersion=project.template_tag, algoCommit=project.template_commit,
        createdAt=project.created_at, updatedAt=project.updated_at, archived=project.archived,
        directory=f"projects/{project.kind}/{project.name}",
        versions=[read_version(version) for version in project.versions],
    )


def get_project_or_404(session: Session, project_id: UUID) -> Project:
    project = session.get(Project, project_id)
    if project is None or project.archived:
        raise HTTPException(404, "项目不存在")
    return project


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
        scheme = templates.select_scheme(body.scheme_version)
        version = next((v for v in templates.list_versions(body.kind, body.scheme_version) if v.tag == body.algo_version), None)
        if version is None:
            raise HTTPException(422, "Algo 版本不存在或不兼容所选 scheme，请刷新版本列表")
        used_suffixes = {identifier.hex[:4] for identifier in session.scalars(
            select(Project.id).where(Project.kind == body.kind)
        )}
        if len(used_suffixes) >= 16 ** 4:
            raise HTTPException(409, "该项目类型的 4 位包名已用完")
        project_id = uuid4()
        while project_id.hex[:4] in used_suffixes:
            project_id = uuid4()
        project = Project(id=project_id, name=body.name, description=body.description, kind=body.kind,
                          template_tag=version.tag, template_commit=version.commit,
                          scheme_version=scheme.tag, scheme_commit=scheme.commit)
        if session.scalar(select(Project.id).where(Project.kind == body.kind, Project.name == body.name)):
            raise HTTPException(409, "该类型下已有同名项目（包含已删除保留的项目）")
        destination = project_directory(project)
        if destination.exists():
            raise HTTPException(409, "同名目录已存在，请使用其他项目名称")
        destination.mkdir(parents=True, exist_ok=False)
        try:
            templates.create_directory(body.kind, version.commit, destination, project.id.hex[:4])
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
    project = get_project_or_404(session, project_id)
    if project.name == body.name:
        project.description = body.description
        session.commit()
        return read_project(project)
    source = project_directory(project)
    destination = source.with_name(body.name)
    if destination.exists() or session.scalar(select(Project.id).where(Project.kind == project.kind, Project.name == body.name)):
        raise HTTPException(409, "该类型下已有同名项目或目录")
    if not source.is_dir():
        raise HTTPException(409, "项目目录不存在，请检查共享卷挂载")
    # 虚拟环境含绝对路径；重命名时重建环境，并保留原环境供失败回滚。
    backup = Path(tempfile.mkdtemp(prefix=".rename-", dir=source.parent))
    kernel_file = kernel_directory(str(project.id)) / "kernel.json"
    old_kernel = kernel_file.read_bytes()
    old_metadata = (source / ".solo").read_bytes()
    old_notebook = (source / "research.ipynb").read_bytes()
    try:
        source.rename(destination)
        (destination / ".venv").rename(backup / ".venv")
        project.name, project.description = body.name, body.description
        write_project_metadata(project, destination)
        prepare_environment(destination, str(project.id), project.name)
        session.commit()
    except Exception as error:
        session.rollback()
        if destination.exists():
            if (backup / ".venv").exists():
                shutil.rmtree(destination / ".venv", ignore_errors=True)
                (backup / ".venv").rename(destination / ".venv")
            destination.rename(source)
            (source / ".solo").write_bytes(old_metadata)
            (source / "research.ipynb").write_bytes(old_notebook)
        kernel_file.write_bytes(old_kernel)
        if isinstance(error, EnvironmentError):
            raise HTTPException(502, str(error)) from error
        raise HTTPException(503, "重命名项目失败，已恢复原目录和环境") from error
    finally:
        shutil.rmtree(backup)
    return read_project(project)


@router.delete("/projects/{project_id}", status_code=204)
def archive_project(project_id: UUID, session: Database) -> Response:
    project = get_project_or_404(session, project_id)
    project.archived = True
    session.commit()
    return Response(status_code=204)


@router.get("/projects/{project_id}/jupyter")
def open_jupyter(project_id: UUID, session: Database) -> RedirectResponse:
    project = get_project_or_404(session, project_id)
    if not project_directory(project).is_dir():
        raise HTTPException(409, "项目目录不存在，请检查共享卷挂载")
    path = quote(f"projects/{project.kind}/{project.name}/research.ipynb", safe="/")
    url = f"{SoloSettings.JUPYTER_URL}/lab/tree/{path}"
    if SoloSettings.JUPYTER_TOKEN:
        url += "?" + urlencode({"token": SoloSettings.JUPYTER_TOKEN})
    return RedirectResponse(url, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
