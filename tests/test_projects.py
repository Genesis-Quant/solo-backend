import hashlib
import json
import re
import subprocess
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
from core.apps import version_policy as policy
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
            f'[project]\nname = "{kind}"\nversion = "1.2.0"\n'
            'dependencies = ["scheme>=1.2.0,<1.3.0"]\n'
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
    monkeypatch.setattr(templates, "list_versions", lambda kind, scheme_version, refresh=False: [TemplateVersion(tag="v1.2.0", commit="a" * 40)])
    monkeypatch.setattr(templates, "list_scheme_versions", lambda refresh=False: [TemplateVersion(tag="v1.2.0", commit="b" * 40)])
    monkeypatch.setattr(templates, "project_metadata", lambda *args: {"version": "1.2.0"})
    monkeypatch.setattr(templates, "create_directory", lambda kind, commit, directory, suffix: templates.unpack_template(archive_content(kind), kind, directory, suffix))

    def prepare(directory, project_id, name, *, locked=False):
        (directory / ".venv").mkdir()
        if not locked:
            (directory / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
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
    response = client.post("/api/v1/projects", json={"name": "测试 项目", "kind": kind, "scheme_version": "v1.2.0", "algo_version": "v1.2.0"})
    assert response.status_code == 201, response.text
    project = response.json()
    directory = root / "projects" / kind / "测试 项目"
    metadata = json.loads((directory / ".solo").read_text(encoding="utf-8"))
    assert metadata == {"project_id": project["id"], "name": "测试 项目", "kind": kind, "scheme_version": "v1.2.0", "scheme_commit": "b" * 40, "algo_version": "v1.2.0", "algo_commit": "a" * 40, "template_package_version": "1.2.0"}
    assert project["directory"] == f"projects/{kind}/测试 项目"
    assert project["schemeVersion"] == "v1.2.0"
    assert project["schemeCommit"] == "b" * 40
    assert project["algoVersion"] == "v1.2.0"
    assert project["algoCommit"] == "a" * 40
    pyproject = tomllib.loads((directory / "pyproject.toml").read_text(encoding="utf-8"))
    suffix = project["id"].replace("-", "")[:4]
    assert pyproject["project"]["name"] == f"{kind}-{suffix}"
    assert (directory / "src" / f"{kind}_{suffix}" / "__init__.py").is_file()
    assert pyproject["project"]["dependencies"] == ["scheme>=1.2.0,<1.3.0"]
    assert pyproject["tool"]["uv"]["sources"]["scheme"]["rev"] == "b" * 40
    assert len(client.get("/api/v1/projects").json()) == 1
    with Session(engine) as session:
        saved = session.scalar(select(Project))
        assert saved.template_tag == "v1.2.0"
        assert saved.template_commit == "a" * 40
        assert saved.scheme_version == "v1.2.0"
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
    assert client.post("/api/v1/projects", json={"name": "新名字", "kind": kind, "scheme_version": "v1.2.0", "algo_version": "v1.2.0"}).status_code == 409


@pytest.mark.parametrize("name", ["../escape", "..", ".hidden", "a/b", "a\\b", "", "  ", "a\nxx"])
def test_invalid_directory_name(projects_client, name):
    client, _, _ = projects_client
    assert client.post("/api/v1/projects", json={"name": name, "kind": "factor", "scheme_version": "v1.2.0", "algo_version": "v1.2.0"}).status_code == 422


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
            "name": name, "kind": "factor", "scheme_version": "v1.2.0", "algo_version": "v1.2.0",
        })
        assert response.status_code == 201, response.text
        data = tomllib.loads((root / "projects" / "factor" / name / "pyproject.toml").read_text())
        assert data["project"]["name"] == f"factor-{suffix}"


def test_missing_tag_and_download_failure(projects_client, monkeypatch):
    client, root, engine = projects_client
    body = {"name": "失败项目", "kind": "model", "scheme_version": "v1.2.0", "algo_version": "missing"}
    assert client.post("/api/v1/projects", json=body).status_code == 422
    body["algo_version"] = "v1.2.0"

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
    project = client.post("/api/v1/projects", json={"name": "原项目", "kind": "factor", "scheme_version": "v1.2.0", "algo_version": "v1.2.0"}).json()

    def fail(*args, **kwargs):
        assert kwargs == {"locked": True}
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
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.0", True, id="minimum"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.7", "1.1.0", True, id="newer-scheme-patch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.7", True, id="older-scheme-patch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.7", "1.1.7", True, id="matching-patch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.999", "1.1.3", True, id="independent-patches"),
    pytest.param(["scheme>=1.0.0,<1.1.0"], "1.0.7", "1.0.0", True, id="another-minor"),
    pytest.param(["scheme>=1.0.0,<1.1.0"], "1.1.0", "1.0.0", False, id="old-on-new"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.0.0", "1.1.0", False, id="new-on-old"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.0.7", "1.1.0", False, id="new-on-old-patch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.2.0", "1.1.0", False, id="later-minor"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.0.7", False, id="earlier-template-minor"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.2.0", False, id="later-template-minor"),
    pytest.param(["scheme>=1.1.5,<1.2.0"], "1.1.0", "1.1.0", False, id="below-patch-minimum"),
    pytest.param(["scheme>=1.1.5,<1.2.0"], "1.1.7", "1.1.7", False, id="restricted-patch-minimum"),
    pytest.param(["pydantic>=2,<3", "ScHeMe (<1.2.0, >=1.1.0)"], "1.1.7", "1.1.0", True, id="order-case-and-other-dependency"),
    pytest.param(["scheme>=0.1.0,<0.2.0"], "0.1.7", "0.1.0", True, id="zero-major"),
    pytest.param(["scheme>=2.1.0,<2.2.0"], "2.1.0", "2.1.7", True, id="another-major"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "2.0.0", "1.1.0", False, id="scheme-major-mismatch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "2.0.0", False, id="template-major-mismatch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "invalid", False, id="invalid-template-version"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1", "1.1", True, id="actual-shorthand"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.7rc1", "1.1.0", False, id="actual-prerelease"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.7.dev1", "1.1.0", False, id="actual-dev"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.7.post1", "1.1.0", False, id="actual-post"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.7+local", "1.1.0", False, id="actual-local"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1!1.1.7", "1.1.0", False, id="actual-epoch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.7.0", "1.1.0", False, id="actual-four-part"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.7rc1", False, id="template-prerelease"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.7.dev1", False, id="template-dev"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.7.post1", False, id="template-post"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.7+local", False, id="template-local"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1!1.1.7", False, id="template-epoch"),
    pytest.param(["scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.7.0", False, id="template-four-part"),
    pytest.param([], "1.1.0", "1.1.0", False, id="missing"),
    pytest.param(["scheme>=1.1.0,<1.2.0", "Scheme>=1.1.0,<1.2.0"], "1.1.0", "1.1.0", False, id="multiple"),
    pytest.param(["scheme @ https://example.invalid/scheme.whl"], "1.1.0", "1.1.0", False, id="url"),
    pytest.param(["scheme @ git+https://example.invalid/scheme.git@deadbeef"], "1.1.0", "1.1.0", False, id="git-url"),
    pytest.param(['scheme>=1.1.0,<1.2.0; python_version >= "3.12"'], "1.1.0", "1.1.0", False, id="marker"),
    pytest.param(["scheme[extra]>=1.1.0,<1.2.0"], "1.1.0", "1.1.0", False, id="extras"),
    pytest.param(["scheme==1.1.0"], "1.1.0", "1.1.0", False, id="exact"),
    pytest.param(["scheme==1.1.*"], "1.1.0", "1.1.0", False, id="wildcard"),
    pytest.param(["scheme>=1.1.0"], "1.1.0", "1.1.0", False, id="missing-upper"),
    pytest.param(["scheme<1.2.0"], "1.1.0", "1.1.0", False, id="missing-lower"),
    pytest.param(["scheme>=1.2.0,<1.2.0"], "1.1.0", "1.1.0", False, id="cross-major-lower"),
    pytest.param(["scheme>=1.1.0,<3.0.0"], "1.1.0", "1.1.0", False, id="cross-major-upper"),
    pytest.param(["scheme>=1.1.0,<2.0.0"], "1.1.0", "1.1.0", False, id="legacy-major-range"),
    pytest.param(["scheme>=1.0.0,<1.2.0"], "1.1.0", "1.1.0", False, id="cross-minor-lower"),
    pytest.param(["scheme>=1.1.0,<1.3.0"], "1.1.0", "1.1.0", False, id="cross-minor-upper"),
    pytest.param(["scheme>=1.1.0,<1.1.9"], "1.1.0", "1.1.0", False, id="narrow-upper"),
    pytest.param(["scheme>=1.1.0,<1.2.7"], "1.1.0", "1.1.0", False, id="nonzero-upper-patch"),
    pytest.param(["scheme>=1.1.0,<=1.2.0"], "1.1.0", "1.1.0", False, id="inclusive-upper"),
    pytest.param(["scheme>1.1.0,<1.2.0"], "1.1.7", "1.1.0", False, id="exclusive-lower"),
    pytest.param(["scheme>=1.1.0,<1.2.0,!=1.1.5"], "1.1.0", "1.1.0", False, id="excluded-version"),
    pytest.param(["scheme~=1.1.0,<1.2.0"], "1.1.0", "1.1.0", False, id="compatible-operator"),
    pytest.param(["scheme~=1.1.0"], "1.1.0", "1.1.0", False, id="compatible-only"),
    pytest.param(["scheme>=1.0.0,>=1.1.0,<1.2.0"], "1.1.0", "1.1.0", False, id="multiple-lower"),
    pytest.param(["scheme>=1.1.0,<1.2.0,<1.3.0"], "1.1.0", "1.1.0", False, id="multiple-upper"),
    pytest.param(["scheme>=1.1.0,>=1.1.0,<1.2.0"], "1.1.0", "1.1.0", False, id="duplicate-lower"),
    pytest.param(["scheme>=1.1.0,<1.2.0,<1.2.0"], "1.1.0", "1.1.0", False, id="duplicate-upper"),
    pytest.param(["scheme>=1,<1.1"], "1.0.7", "1.0.0", True, id="zero-minor-shorthand"),
    pytest.param(["scheme>=1.1,<1.2"], "1.1.0", "1.1.7", True, id="shorthand"),
    pytest.param(["scheme>=1.1,<1.2"], "1.0.7", "1.1.0", False, id="shorthand-below-minimum"),
    pytest.param(["scheme>=1.1,<1.2.0"], "1.1.0", "1.1.0", True, id="shorthand-lower"),
    pytest.param(["scheme>=1.1.0,<1.2"], "1.1.7", "1.1.0", True, id="shorthand-upper"),
    pytest.param(["scheme>=1.1,<2"], "1.1.0", "1.1.0", False, id="legacy-shorthand"),
    pytest.param(["scheme>=1.1.0rc1,<1.2.0"], "1.1.0", "1.1.0", False, id="prerelease-lower"),
    pytest.param(["scheme>=1.1.0.dev1,<1.2.0"], "1.1.0", "1.1.0", False, id="dev-lower"),
    pytest.param(["scheme>=1.1.0.post1,<1.2.0"], "1.1.7", "1.1.0", False, id="post-lower"),
    pytest.param(["scheme>=1.1.0+local,<1.2.0"], "1.1.0", "1.1.0", False, id="local-lower"),
    pytest.param(["scheme>=1!1.1.0,<1.2.0"], "1.1.0", "1.1.0", False, id="epoch-lower"),
    pytest.param(["scheme>=1.1.0.0,<1.2.0"], "1.1.0", "1.1.0", False, id="four-part-lower"),
    pytest.param(["scheme>=1.1.0,<1.2.0rc1"], "1.1.0", "1.1.0", False, id="prerelease-upper"),
    pytest.param(["scheme>=not-a-version,<1.2.0"], "1.1.0", "1.1.0", False, id="malformed"),
]


@pytest.fixture
def template_metadata(monkeypatch):
    # Pure compatibility-contract cases intentionally exercise older/future series;
    # admission itself is covered separately with the real policy, never globally disabled.
    compatibility_policy = {**policy.load_policy(), "minimum_versions": {"scheme": "0.0.0", "template": "0.0.0"},
                            "retired_releases": {"scheme": [], "template": []}}
    monkeypatch.setattr(policy, "load_policy", lambda: compatibility_policy)
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
    if not compatible and not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", scheme_version):
        with pytest.raises(ValueError):
            templates.list_versions("factor", selected.tag)
        return
    assert templates.list_versions("factor", selected.tag) == ([release.model_copy(update={"version": project_version})] if compatible else [])


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
    selected = TemplateVersion(tag="v1.2.0", commit="b" * 40)
    releases = [TemplateVersion(tag=f"v1.{minor}.0", commit=str(minor) * 40) for minor in (4, 3, 2)]
    metadata = {
        releases[0].commit: {"version": "1.4.0", "dependencies": ["scheme>=invalid"]},
        releases[1].commit: {"version": "1.3.0", "dependencies": ["scheme>=1.3.0,<1.4.0"]},
        releases[2].commit: {"version": "1.2.7", "dependencies": ["scheme>=1.2.0,<1.3.0"]},
    }
    monkeypatch.setattr(templates, "select_scheme", lambda tag: selected)
    monkeypatch.setattr(templates, "repository_versions", lambda *args: releases)
    monkeypatch.setattr(templates, "project_metadata", lambda repository, commit, path: (
        {"version": "1.2.0"} if repository == SoloSettings.SCHEME_REPOSITORY else metadata[commit]
    ))
    assert templates.list_versions("factor", selected.tag) == [releases[2].model_copy(update={"version": "1.2.7"})]


def directory_digest(directory):
    return {str(path.relative_to(directory)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in directory.rglob("*") if path.is_file()}


@pytest.mark.parametrize("failure", ["uv-conflict", "retired-postcheck", "changed-source"])
def test_rename_locked_environment_failure_restores_exact_source_tree(projects_client, monkeypatch, failure):
    from core.apps.projects import environment

    client, root, engine = projects_client
    project = client.post("/api/v1/projects", json={
        "name": "original", "kind": "model", "scheme_version": "v1.2.0", "algo_version": "v1.2.0",
    }).json()
    source = root / "projects/model/original"
    (source / ".venv/installed.txt").write_bytes(b"original exact environment")
    # The TOML was edited without re-locking. Renaming must validate this input
    # with --locked, not resolve it and silently replace the active old lock.
    path = source / "pyproject.toml"
    if failure == "uv-conflict":
        path.write_text(path.read_text(encoding="utf-8").replace(
            "scheme>=1.2.0,<1.3.0", "scheme>=1.1.0,<1.2.0"), encoding="utf-8")
    before = directory_digest(source)
    kernel = kernel_directory(project["id"]) / "kernel.json"
    old_kernel = kernel.read_bytes()
    old_lock = (source / "uv.lock").read_bytes()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        assert args[:2] == ["uv", "sync"]
        assert "--locked" in args
        renamed = Path(args[args.index("--project") + 1])
        assert (renamed / "uv.lock").read_bytes() == old_lock
        (renamed / ".venv").mkdir()
        (renamed / ".venv/partial.txt").write_text("must be discarded")
        if failure == "uv-conflict":
            # Model even a faulty installer that writes before returning failure.
            (renamed / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.1.7"\n')
            (renamed / "research.ipynb").write_text('{"metadata":{"changed":true}}')
            kernel.write_text("changed")
            return subprocess.CompletedProcess(args, 1, "", "lockfile needs to be updated, but --locked was provided")
        if failure == "retired-postcheck":
            (renamed / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.1.7"\n')
        else:
            pyproject = renamed / "pyproject.toml"
            pyproject.write_text(pyproject.read_text(encoding="utf-8").replace('b' * 40, 'c' * 40), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    def rebuild(directory, project_id, name, *, locked=False):
        if failure == "uv-conflict":
            return environment.prepare_environment(directory, project_id, name, locked=locked)
        assert locked is True
        run(["uv", "sync", "--project", str(directory), "--locked"])
        kernel.write_text("changed")
        (directory / "research.ipynb").write_text('{"metadata":{"changed":true}}')

    monkeypatch.setattr(environment.subprocess, "run", run)
    monkeypatch.setattr(views, "prepare_environment", rebuild)
    response = client.patch(f"/api/v1/projects/{project['id']}", json={"name": "renamed", "description": "new"})
    assert response.status_code == {"uv-conflict": 502, "retired-postcheck": 422, "changed-source": 503}[failure], response.text
    if failure == "retired-postcheck":
        assert response.json()["detail"]["code"] == "version_retired"
    assert len(calls) == 1
    assert directory_digest(source) == before
    assert kernel.read_bytes() == old_kernel
    assert not source.with_name("renamed").exists()
    assert not list(source.parent.glob(".rename-*"))
    with Session(engine) as session:
        saved = session.get(Project, UUID(project["id"]))
        assert saved.name == "original" and saved.description == ""


@pytest.mark.parametrize("tag", ["stable-current", "v1.1.42"])
def test_creation_metadata_custom_or_misleading_tags_stays_active_offline(projects_client, monkeypatch, tag):
    client, root, engine = projects_client
    scheme = TemplateVersion(tag=tag, commit="b" * 40)
    release = TemplateVersion(tag=tag, commit="a" * 40)
    monkeypatch.setattr(templates, "list_scheme_versions", lambda refresh=False: [scheme.model_copy(update={"version": "1.2.0"})])
    monkeypatch.setattr(templates, "list_versions", lambda *args: [release.model_copy(update={"version": "1.2.0"})])
    response = client.post("/api/v1/projects", json={
        "name": "metadata", "kind": "factor", "scheme_version": tag, "algo_version": tag,
    })
    assert response.status_code == 201, response.text
    identifier = response.json()["id"]
    assert response.json()["retired"] is False
    with Session(engine) as session:
        saved = session.get(Project, UUID(identifier))
        assert saved.template_package_version == "1.2.0"
        assert policy.project_sources(saved)["template"] == {"tag": tag, "commit": "a" * 40, "version": "1.2.0"}
    # Editing a candidate patch is not a change of the creation template release.
    path = root / "projects/factor/metadata/pyproject.toml"
    path.write_text(path.read_text(encoding="utf-8").replace('version = "1.2.0"', 'version = "1.2.99"'), encoding="utf-8")

    def offline(*args, **kwargs):
        raise AssertionError("project reads/check/save must not fetch remote metadata")

    monkeypatch.setattr(templates, "project_metadata", offline)
    assert client.get(f"/api/v1/projects/{identifier}").json()["retired"] is False
    assert client.get("/api/v1/projects").json()[0]["retired"] is False
    assert client.post(f"/api/v1/version-policy/projects/{identifier}/check").json() == {"allowed": True}
    saved = client.post(f"/api/v1/projects/{identifier}/versions", json={})
    assert saved.status_code == 201, saved.text
    assert saved.json()["packageVersion"] == "1.2.1"


@pytest.mark.parametrize("scheme_tag,template_tag", [("stable-current", "stable-current"), ("v1.1.42", "v1.1.42")])
def test_catalog_uses_exact_identity_then_actual_metadata(monkeypatch, scheme_tag, template_tag):
    scheme = TemplateVersion(tag=scheme_tag, commit="b" * 40)
    release = TemplateVersion(tag=template_tag, commit="a" * 40)
    monkeypatch.setattr(templates, "repository_versions", lambda repository, refresh=False:
                        [scheme] if repository == SoloSettings.SCHEME_REPOSITORY else [release])
    calls = []

    def metadata(repository, commit, path):
        calls.append((commit, path))
        return {"version": "1.2.0", "dependencies": ["scheme>=1.2.0,<1.3.0"]}

    monkeypatch.setattr(templates, "project_metadata", metadata)
    assert templates.select_scheme(scheme.tag).version == "1.2.0"
    assert templates.list_versions("factor", scheme.tag) == [release.model_copy(update={"version": "1.2.0"})]
    assert (release.commit, "factor/pyproject.toml") in calls


@pytest.mark.parametrize("kind", ["scheme", "template"])
@pytest.mark.parametrize("identity", ["tag", "commit"])
def test_catalog_known_retired_identity_cannot_be_moved_to_active_metadata(monkeypatch, kind, identity):
    retired = policy.load_policy()["retired_releases"][kind][0]
    release = TemplateVersion(tag=retired["tag"] if identity == "tag" else "stable-current",
                              commit=retired["commit"] if identity == "commit" else "f" * 40)
    monkeypatch.setattr(templates, "repository_versions", lambda *args: [release])

    def metadata(*args):
        raise AssertionError("known retired identities must be excluded before metadata")

    monkeypatch.setattr(templates, "project_metadata", metadata)
    if kind == "scheme":
        assert templates.list_scheme_versions() == []
    else:
        monkeypatch.setattr(templates, "select_scheme", lambda *args: TemplateVersion(tag="v1.2.0", commit="b" * 40, version="1.2.0"))
        assert templates.list_versions("factor", "v1.2.0") == []


@pytest.mark.parametrize("kind", ["scheme", "template"])
def test_custom_tag_actual_metadata_below_floor_is_still_rejected(monkeypatch, kind):
    active = TemplateVersion(tag="stable-current", commit="b" * 40, version="1.2.0")
    monkeypatch.setattr(templates, "repository_versions", lambda *args: [active])
    monkeypatch.setattr(templates, "project_metadata", lambda *args: {
        "version": "1.1.42", "dependencies": ["scheme>=1.1.0,<1.2.0"],
    })
    if kind == "scheme":
        assert templates.list_scheme_versions() == []
    else:
        monkeypatch.setattr(templates, "select_scheme", lambda *args: active)
        assert templates.list_versions("factor", active.tag) == []
