"""Explicit environment upgrades without migrating user source or historical runs."""

import json
import tomllib
from uuid import UUID

import requests
from fastapi import HTTPException
from packaging.utils import canonicalize_name
from packaging.version import Version
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy

from . import deletion, templates, upgrade_environment
from .environment import EnvironmentError
from .models import Project, ProjectDeletion, ProjectVersion
from .schemas import ProjectUpgrade

FIELDS = ("scheme_version", "scheme_commit", "template_tag", "template_commit", "template_package_version")


def _target(project: Project, body: ProjectUpgrade):
    scheme = templates.select_scheme(body.scheme_version)
    algo = next((item for item in templates.list_versions(project.kind, scheme.tag)
                 if item.tag == body.algo_version), None)
    if algo is None:
        raise ValueError("Algo 版本不存在、已退役或与所选 Scheme 不兼容")
    content = templates.git_file(SoloSettings.TEMPLATE_REPOSITORY, algo.commit, f"{project.kind}/pyproject.toml")
    document = tomllib.loads(content.decode("utf-8"))
    actual = document["project"]["version"]
    policy.require_release("template", tag=algo.tag, commit=algo.commit, version=actual)
    if Version(algo.version) != Version(actual):
        raise ValueError("Algo catalog 与模板实际版本不一致")
    return scheme, document, {
        "scheme_version": scheme.tag, "scheme_commit": scheme.commit,
        "template_tag": algo.tag, "template_commit": algo.commit, "template_package_version": actual,
    }


def _upgrade(project_id: UUID, body: ProjectUpgrade, session: Session) -> Project:
    project = session.scalar(select(Project).where(Project.id == project_id).with_for_update()
                             .execution_options(populate_existing=True))
    if project is None or project.archived:
        raise HTTPException(404, "项目不存在")
    if session.get(ProjectDeletion, project_id) is not None:
        raise HTTPException(409, "项目正在删除，不能升级")
    if session.scalar(select(ProjectVersion.id).where(
            ProjectVersion.project_id == project_id, ProjectVersion.status.in_(deletion.ACTIVE_PHASES))) is not None:
        raise HTTPException(409, "项目仍有构建中或运行中的研究任务，不能升级")
    scheme, template, target = _target(project, body)
    if all(getattr(project, name) == target[name] for name in FIELDS):
        return project
    try:
        response = deletion.jupyter_request("/solo/project/operations", deletion.jupyter_headers(),
                                            params={"project_id": str(project_id)})
        response.raise_for_status()
    except requests.RequestException as error:
        raise HTTPException(503, "无法确认 Jupyter 项目操作状态，请稍后重试") from error
    if response.json()["busy"]:
        raise HTTPException(409, "项目正在安装依赖、读取参数或保存版本，请稍后再升级")

    directory = policy.project_path(project)
    if not deletion.checked_directory(directory, SoloSettings.SHARED_DIR):
        raise HTTPException(409, "项目目录不存在，不能升级")
    for name in ("pyproject.toml", "uv.lock", ".solo"):
        if not (directory / name).resolve().is_relative_to(directory.resolve()):
            raise HTTPException(409, "项目配置路径越界，不能覆盖其他文件")
    metadata = deletion.read_metadata(directory / ".solo")
    deletion.require_identity(metadata, {
        **deletion.identity(project.id, project.name, project.kind), "package_name": project.package_name,
    })
    original = {name: (directory / name).read_bytes() for name in ("pyproject.toml", "uv.lock", ".solo")}
    config = tomllib.loads(original["pyproject.toml"].decode("utf-8"))
    if canonicalize_name(config["project"]["name"]) != canonicalize_name(project.package_name):
        raise HTTPException(409, "pyproject 包身份与项目不一致")
    updated = upgrade_environment.updated_project_config(original["pyproject.toml"], template, scheme)
    metadata.setdefault("source_template", {
        "algo_version": project.template_tag, "algo_commit": project.template_commit,
        "template_package_version": project.template_package_version,
    })
    metadata.update({
        "scheme_version": scheme.tag, "scheme_commit": scheme.commit,
        "algo_version": target["template_tag"], "algo_commit": target["template_commit"],
        "template_package_version": target["template_package_version"],
    })
    deletion.stop_jupyter(project.id, f"projects/{project.kind}/{project.name}")
    try:
        (directory / "pyproject.toml").write_bytes(updated)
        upgrade_environment.install_environment(directory)
        (directory / ".solo").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        for name in FIELDS:
            setattr(project, name, target[name])
        session.commit()
    except Exception:
        try:
            for name, content in original.items():
                (directory / name).write_bytes(content)
            upgrade_environment.install_environment(directory)
        finally:
            session.rollback()
        raise
    return project


def upgrade_project(project_id: UUID, body: ProjectUpgrade, session: Session) -> Project:
    with deletion.project_lifecycle_guard(project_id, session):
        try:
            return _upgrade(project_id, body, session)
        except policy.VersionPolicyError as error:
            raise HTTPException(422, error.detail) from error
        except ValueError as error:
            raise HTTPException(422, str(error)) from error
        except (templates.TemplateError, EnvironmentError) as error:
            raise HTTPException(502, str(error)) from error
