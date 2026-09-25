from io import BytesIO
import json
import tomllib
from pathlib import Path
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from config import SoloSettings
from core.apps.projects import templates, views
from core.apps.projects.environment import EnvironmentError, kernel_directory
from core.apps.projects.models import Project
from core.apps.projects.schemas import TemplateVersion
from core.database.base import Base
from core.database.session import get_database_session
from main import app


def archive_content(kind: str, extra: str | None = None) -> bytes:
    result = BytesIO()
    with ZipFile(result, "w") as archive:
        archive.writestr(f"repo/{kind}/pyproject.toml", f'[project]\nname = "{kind}"\nversion = "0.1.0"\n')
        archive.writestr(f"repo/{kind}/research.ipynb", '{"cells": [], "metadata": {}}')
        archive.writestr(f"repo/{kind}/src/{kind}/__init__.py", "")
        if extra:
            archive.writestr(extra, "unsafe")
    return result.getvalue()


@pytest.fixture
def projects_client(tmp_path: Path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(SoloSettings, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(SoloSettings, "JUPYTER_TOKEN", "")
    monkeypatch.setattr(templates, "list_versions", lambda kind, scheme_version, refresh=False: [TemplateVersion(tag="v0.1.0", commit="a" * 40)])
    monkeypatch.setattr(templates, "list_scheme_versions", lambda refresh=False: [TemplateVersion(tag="v0.1.0", commit="b" * 40)])
    monkeypatch.setattr(templates, "project_metadata", lambda *args: {"version": "0.1.0"})
    monkeypatch.setattr(templates, "create_directory", lambda kind, commit, directory, suffix: templates.unpack_template(archive_content(kind), kind, directory, suffix))

    def prepare(directory, project_id, name):
        (directory / ".venv").mkdir()
        kernel = kernel_directory(project_id)
        kernel.mkdir(parents=True, exist_ok=True)
        (kernel / "kernel.json").write_text(json.dumps({"display_name": name}))

    monkeypatch.setattr(views, "prepare_environment", prepare)

    def session():
        with Session(engine, expire_on_commit=False) as database:
            yield database

    app.dependency_overrides[get_database_session] = session
    try:
        with TestClient(app) as client:
            yield client, tmp_path, engine
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution"])
def test_create_read_rename_archive(projects_client, kind):
    client, root, engine = projects_client
    response = client.post("/api/v1/projects", json={"name": "测试 项目", "kind": kind, "scheme_version": "v0.1.0", "algo_version": "v0.1.0"})
    assert response.status_code == 201, response.text
    project = response.json()
    directory = root / "projects" / kind / "测试 项目"
    metadata = json.loads((directory / ".solo").read_text(encoding="utf-8"))
    assert metadata == {"project_id": project["id"], "name": "测试 项目", "kind": kind, "scheme_version": "v0.1.0", "scheme_commit": "b" * 40, "algo_version": "v0.1.0", "algo_commit": "a" * 40}
    assert project["directory"] == f"projects/{kind}/测试 项目"
    assert project["schemeVersion"] == "v0.1.0"
    assert project["schemeCommit"] == "b" * 40
    assert project["algoVersion"] == "v0.1.0"
    assert project["algoCommit"] == "a" * 40
    pyproject = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["project"]["dependencies"] == ["scheme>=0.0.0,<1.0.0"]
    assert pyproject["tool"]["uv"]["sources"]["scheme"]["rev"] == "b" * 40
    assert len(client.get("/api/v1/projects").json()) == 1
    with Session(engine) as session:
        saved = session.scalar(select(Project))
        assert saved.template_tag == "v0.1.0"
        assert saved.template_commit == "a" * 40
        assert saved.scheme_version == "v0.1.0"
        assert saved.scheme_commit == "b" * 40
    url = f"/api/v1/projects/{project['id']}"
    redirect = client.get(url + "/jupyter", follow_redirects=False)
    assert redirect.status_code == 307
    assert f"/lab/tree/projects/{kind}/%E6%B5%8B%E8%AF%95%20%E9%A1%B9%E7%9B%AE/research.ipynb" in redirect.headers["location"]
    assert client.patch(url, json={"name": "新名字", "description": "修改描述"}).status_code == 200
    renamed = directory.with_name("新名字")
    assert not directory.exists()
    assert json.loads((renamed / ".solo").read_text(encoding="utf-8"))["name"] == "新名字"
    assert client.delete(url).status_code == 204
    assert client.get("/api/v1/projects").json() == []
    assert client.get(url).status_code == 404
    assert renamed.is_dir()
    assert client.post("/api/v1/projects", json={"name": "新名字", "kind": kind, "scheme_version": "v0.1.0", "algo_version": "v0.1.0"}).status_code == 409


@pytest.mark.parametrize("name", ["../escape", "..", ".hidden", "a/b", "a\\b", "", "  ", "a\nxx"])
def test_invalid_directory_name(projects_client, name):
    client, _, _ = projects_client
    assert client.post("/api/v1/projects", json={"name": name, "kind": "factor", "scheme_version": "v0.1.0", "algo_version": "v0.1.0"}).status_code == 422


def test_missing_tag_and_download_failure(projects_client, monkeypatch):
    client, root, engine = projects_client
    body = {"name": "失败项目", "kind": "model", "scheme_version": "v0.1.0", "algo_version": "missing"}
    assert client.post("/api/v1/projects", json=body).status_code == 422
    body["algo_version"] = "v0.1.0"

    def fail(*args):
        raise EnvironmentError("依赖无法安装")

    monkeypatch.setattr(views, "prepare_environment", fail)
    response = client.post("/api/v1/projects", json=body)
    assert response.status_code == 502
    assert not (root / "projects" / "model" / "失败项目").exists()
    with Session(engine) as session:
        assert session.scalar(select(Project)) is None


def test_rename_failure_restores_files_and_database(projects_client, monkeypatch):
    client, root, _ = projects_client
    project = client.post("/api/v1/projects", json={"name": "原项目", "kind": "factor", "scheme_version": "v0.1.0", "algo_version": "v0.1.0"}).json()

    def fail(*args):
        raise EnvironmentError("安装失败")

    monkeypatch.setattr(views, "prepare_environment", fail)
    response = client.patch(f"/api/v1/projects/{project['id']}", json={"name": "新项目"})
    assert response.status_code == 502
    assert (root / "projects/factor/原项目/.venv").is_dir()
    assert not (root / "projects/factor/新项目").exists()
    assert client.get("/api/v1/projects").json()[0]["name"] == "原项目"


def test_reject_archive_path_traversal(tmp_path):
    with pytest.raises(templates.TemplateError):
        templates.unpack_template(archive_content("factor", "repo/factor/../../escape"), "factor", tmp_path, "id")
    assert not (tmp_path.parent / "escape").exists()
