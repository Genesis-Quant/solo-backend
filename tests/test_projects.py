import json
import tomllib
from io import BytesIO
from pathlib import Path
from uuid import UUID
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
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
        archive.writestr(f"repo/{kind}/pyproject.toml", (
            f'[project]\nname = "{kind}"\nversion = "0.1.0"\n'
            'dependencies = ["scheme>=0.1.0,<1.0.0"]\n'
        ))
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
    suffix = project["id"].replace("-", "")[:4]
    assert pyproject["project"]["name"] == f"{kind}-{suffix}"
    assert (directory / "src" / f"{kind}_{suffix}" / "__init__.py").is_file()
    assert pyproject["project"]["dependencies"] == ["scheme>=0.1.0,<1.0.0"]
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


def test_short_package_suffix_skips_existing_project(projects_client, monkeypatch):
    client, root, _ = projects_client
    identifiers = iter(UUID(value) for value in (
        "abcd0000-0000-4000-8000-000000000001",
        "abcd0000-0000-4000-8000-000000000002",
        "ef120000-0000-4000-8000-000000000003",
    ))
    monkeypatch.setattr(views, "uuid4", lambda: next(identifiers))
    for name, suffix in (("first", "abcd"), ("second", "ef12")):
        response = client.post("/api/v1/projects", json={
            "name": name, "kind": "factor", "scheme_version": "v0.1.0", "algo_version": "v0.1.0",
        })
        assert response.status_code == 201, response.text
        data = tomllib.loads((root / "projects" / "factor" / name / "pyproject.toml").read_text())
        assert data["project"]["name"] == f"factor-{suffix}"


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


SCHEME_COMPATIBILITY = [
    pytest.param(["scheme>=1.0.0,<2.0.0"], "1.0.0", "1.0.1", True, id="old-minimum"),
    pytest.param(["scheme>=1.0.0,<2.0.0"], "1.0.1", "1.0.1", True, id="old-patch"),
    pytest.param(["scheme>=1.0.0,<2.0.0"], "1.1.0", "1.0.1", True, id="old-on-new"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "1.0.0", "1.1.0", False, id="new-on-old"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "1.0.1", "1.1.0", False, id="new-on-old-patch"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "1.1.0", "1.1.0", True, id="new-minimum"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "1.9.9", "1.1.0", True, id="later-minor"),
    pytest.param(["scheme>=1.1.1,<2.0.0"], "1.1.0", "1.1.0", False, id="below-patch-minimum"),
    pytest.param(["scheme>=1.1.1,<2.0.0"], "1.1.1", "1.1.0", True, id="patch-minimum"),
    pytest.param(["pydantic>=2,<3", "ScHeMe (<2.0.0, >=1.1.0)"], "1.1.0", "1.2.0", True, id="order-case-and-other-dependency"),
    pytest.param(["scheme>=2.1.0,<3.0.0"], "2.1.0", "2.0.0", True, id="another-major"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "2.0.0", "1.1.0", False, id="scheme-major-mismatch"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "1.1.0", "2.0.0", False, id="template-major-mismatch"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "1.1.0", "invalid", False, id="invalid-template-version"),
    pytest.param([], "1.1.0", "1.1.0", False, id="missing"),
    pytest.param(["scheme>=1.1.0,<2.0.0", "Scheme>=1.0.0,<2.0.0"], "1.1.0", "1.1.0", False, id="multiple"),
    pytest.param(["scheme @ https://example.invalid/scheme.whl"], "1.1.0", "1.1.0", False, id="url"),
    pytest.param(["scheme @ git+https://example.invalid/scheme.git@deadbeef"], "1.1.0", "1.1.0", False, id="git-url"),
    pytest.param(['scheme>=1.1.0,<2.0.0; python_version >= "3.12"'], "1.1.0", "1.1.0", False, id="marker"),
    pytest.param(["scheme[extra]>=1.1.0,<2.0.0"], "1.1.0", "1.1.0", False, id="extras"),
    pytest.param(["scheme==1.1.0"], "1.1.0", "1.1.0", False, id="exact"),
    pytest.param(["scheme>=1.1.0"], "1.1.0", "1.1.0", False, id="missing-upper"),
    pytest.param(["scheme<2.0.0"], "1.1.0", "1.1.0", False, id="missing-lower"),
    pytest.param(["scheme>=0.1.0,<2.0.0"], "1.1.0", "1.1.0", False, id="cross-major-lower"),
    pytest.param(["scheme>=1.1.0,<3.0.0"], "1.1.0", "1.1.0", False, id="cross-major-upper"),
    pytest.param(["scheme>=1.1.0,<1.9.0"], "1.1.0", "1.1.0", False, id="narrow-upper"),
    pytest.param(["scheme>=1.1.0,<=2.0.0"], "1.1.0", "1.1.0", False, id="inclusive-upper"),
    pytest.param(["scheme>1.0.0,<2.0.0"], "1.1.0", "1.1.0", False, id="exclusive-lower"),
    pytest.param(["scheme>=1.1.0,<2.0.0,!=1.2.0"], "1.1.0", "1.1.0", False, id="excluded-version"),
    pytest.param(["scheme~=1.1.0,<2.0.0"], "1.1.0", "1.1.0", False, id="compatible-operator"),
    pytest.param(["scheme>=1.0.0,>=1.1.0,<2.0.0"], "1.1.0", "1.1.0", False, id="multiple-lower"),
    pytest.param(["scheme>=1.1.0,<2.0.0,<3.0.0"], "1.1.0", "1.1.0", False, id="multiple-upper"),
    pytest.param(["scheme>=1.1.0,<2.0.0,<2.0.0"], "1.1.0", "1.1.0", False, id="duplicate-upper"),
    pytest.param(["scheme>=1,<2"], "1.0.1", "1.0.1", True, id="old-shorthand"),
    pytest.param(["scheme>=1.1,<2"], "1.1.0", "1.1.0", True, id="shorthand"),
    pytest.param(["scheme>=1.1,<2"], "1.0.1", "1.1.0", False, id="shorthand-below-minimum"),
    pytest.param(["scheme>=1.1,<2.0.0"], "1.1.0", "1.1.0", True, id="shorthand-lower"),
    pytest.param(["scheme>=1.1.0,<2"], "1.1.0", "1.1.0", True, id="shorthand-upper"),
    pytest.param(["scheme>=1.1.0rc1,<2.0.0"], "1.1.0", "1.1.0", False, id="prerelease-lower"),
    pytest.param(["scheme>=1.1.0.dev1,<2.0.0"], "1.1.0", "1.1.0", False, id="dev-lower"),
    pytest.param(["scheme>=1.1.0.post1,<2.0.0"], "1.1.1", "1.1.0", False, id="post-lower"),
    pytest.param(["scheme>=1.1.0+local,<2.0.0"], "1.1.0", "1.1.0", False, id="local-lower"),
    pytest.param(["scheme>=1!1.1.0,<2.0.0"], "1.1.0", "1.1.0", False, id="epoch-lower"),
    pytest.param(["scheme>=1.1.0.0,<2.0.0"], "1.1.0", "1.1.0", False, id="four-part-lower"),
    pytest.param(["scheme>=1.1.0,<2.0.0rc1"], "1.1.0", "1.1.0", False, id="prerelease-upper"),
    pytest.param(["scheme>=not-a-version,<2.0.0"], "1.1.0", "1.1.0", False, id="malformed"),
]


@pytest.fixture
def template_metadata(monkeypatch):
    # Tag 不代表实际 metadata version；所有远端读取均由内存数据替代。
    selected = TemplateVersion(tag="v9.9.9", commit="b" * 40)
    release = TemplateVersion(tag="v8.8.8", commit="a" * 40)
    scheme = {}
    project = {}
    monkeypatch.setattr(templates, "repository_versions", lambda repository, refresh=False: (
        [selected] if repository == SoloSettings.SCHEME_REPOSITORY else [release]
    ))
    monkeypatch.setattr(templates, "project_metadata", lambda repository, commit, path: (
        scheme if repository == SoloSettings.SCHEME_REPOSITORY else project
    ))
    return selected, release, scheme, project


@pytest.mark.parametrize("dependencies,scheme_version,project_version,compatible", SCHEME_COMPATIBILITY)
def test_template_version_compatibility(template_metadata, dependencies, scheme_version, project_version, compatible):
    selected, release, scheme, project = template_metadata
    scheme["version"] = scheme_version
    project.update(version=project_version, dependencies=dependencies)
    assert templates.list_versions("factor", selected.tag) == ([release] if compatible else [])


@pytest.mark.parametrize("dependencies,scheme_version,project_version,compatible", SCHEME_COMPATIBILITY)
def test_direct_pin_compatibility(tmp_path, template_metadata, dependencies, scheme_version, project_version, compatible):
    selected, _, scheme, _ = template_metadata
    scheme["version"] = scheme_version
    path = tmp_path / "pyproject.toml"
    original = (
        f'[project]\nname = "factor-demo"\nversion = "{project_version}"\n'
        f'dependencies = {json.dumps(dependencies)} # keep original bounds\n'
        '\n[tool.uv.sources]\n'
        'scheme = { git = "https://example.invalid/old.git", rev = "old" }\n'
        'other = { path = "../other" }\n'
    )
    path.write_text(original, encoding="utf-8")
    if not compatible:
        with pytest.raises(ValueError):
            templates.pin_scheme(tmp_path, selected)
        assert path.read_text(encoding="utf-8") == original
        return
    templates.pin_scheme(tmp_path, selected)
    actual = path.read_text(encoding="utf-8")
    before, after = tomllib.loads(original), tomllib.loads(actual)
    before["tool"]["uv"]["sources"]["scheme"] = {
        "git": f"https://gitee.com/{SoloSettings.SCHEME_REPOSITORY}", "rev": selected.commit,
    }
    assert after == before
    assert actual.split("[tool.uv.sources]")[0] == original.split("[tool.uv.sources]")[0]


def test_template_selection_skips_invalid_release_and_keeps_compatible(monkeypatch):
    selected = TemplateVersion(tag="v1.1.0", commit="b" * 40)
    releases = [TemplateVersion(tag=f"v1.{minor}.0", commit=str(minor) * 40) for minor in (3, 2, 1)]
    metadata = {
        releases[0].commit: {"version": "1.3.0", "dependencies": ["scheme>=invalid"]},
        releases[1].commit: {"version": "1.2.0", "dependencies": ["scheme>=1.2.0,<2.0.0"]},
        releases[2].commit: {"version": "1.1.0", "dependencies": ["scheme>=1.1.0,<2.0.0"]},
    }
    monkeypatch.setattr(templates, "select_scheme", lambda tag: selected)
    monkeypatch.setattr(templates, "repository_versions", lambda *args: releases)
    monkeypatch.setattr(templates, "project_metadata", lambda repository, commit, path: (
        {"version": "1.1.0"} if repository == SoloSettings.SCHEME_REPOSITORY else metadata[commit]
    ))
    assert templates.list_versions("factor", selected.tag) == [releases[2]]
