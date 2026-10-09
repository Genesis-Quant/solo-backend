import hashlib
import json
import re
import subprocess
import tomllib
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.projects import deletion, templates, views
from core.apps.projects.environment import EnvironmentError, kernel_directory
from core.apps.projects.models import Project, ProjectDeletion, ProjectVersion
from core.apps.strategies.models import Strategy
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
    # File-backed SQLite gives concurrent request sessions independent connections.
    engine = create_engine(f"sqlite:///{(tmp_path / 'projects.sqlite3').as_posix()}",
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(SoloSettings, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(SoloSettings, "JUPYTER_TOKEN", "")

    def jupyter(path, headers, *, params=None, method="GET", body=None):
        payload = {"project_id": body["project_id"], "lease": body["lease"]} if method == "POST" else []
        return SimpleNamespace(status_code=204 if method == "DELETE" else 200, json=lambda: payload)

    monkeypatch.setattr(deletion, "jupyter_request", jupyter)
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
        (kernel / "kernel.json").write_text(json.dumps({
            "display_name": name, "argv": [str(directory / ".venv" / "bin" / "python")],
        }))

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
    assert metadata == {"project_id": project["id"], "name": "测试 项目", "kind": kind, "scheme_version": "v1.2.0", "scheme_commit": "b" * 40, "algo_version": "v1.2.0", "algo_commit": "a" * 40, "template_package_version": "1.2.0", "package_name": project["packageName"]}
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
    assert not renamed.exists()
    assert not kernel_directory(project["id"]).exists()
    with Session(engine) as session:
        assert session.get(Project, UUID(project["id"])) is None
    assert client.post("/api/v1/projects", json={"name": "新名字", "kind": kind, "scheme_version": "v1.2.0", "algo_version": "v1.2.0"}).status_code == 201


@pytest.fixture
def idle_jupyter(monkeypatch):
    calls = []

    def get(path, headers, *, params=None, method="GET", body=None):
        calls.append((path, headers))
        payload = {"project_id": body["project_id"], "lease": body["lease"]} if method == "POST" else []
        return SimpleNamespace(status_code=204 if method == "DELETE" else 200, json=lambda: payload)

    monkeypatch.setattr(deletion, "jupyter_request", get)
    return calls


def create_cleanup_project(client, name="cleanup", kind="factor"):
    response = client.post("/api/v1/projects", json={
        "name": name, "kind": kind, "scheme_version": "v1.2.0", "algo_version": "v1.2.0",
    })
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution"])
def test_delete_project_removes_own_runs_and_preserves_other_resources(projects_client, idle_jupyter, kind):
    client, root, engine = projects_client
    project = create_cleanup_project(client, kind=kind)
    other = create_cleanup_project(client, name="unrelated", kind=kind)
    directory = root / project["directory"]
    kernel = kernel_directory(project["id"])
    run_id = uuid4()
    report = root / "runs" / str(run_id) / "report"
    report.mkdir(parents=True)
    manifest = {"status": "success", "reports": {"factor": "result.parquet"}}
    (report / "run.json").write_text(json.dumps(manifest))
    (report / "result.parquet").write_bytes(b"PAR1testPAR1")
    (report.parent / "package.whl").write_bytes(b"frozen wheel")
    with Session(engine) as session:
        session.add(ProjectVersion(id=run_id, project_id=UUID(project["id"]), number=1,
                                   package_version="1.2.1", status="success"))
        session.commit()
    url = f"/api/v1/projects/{project['id']}"
    response = client.delete(url + "?delete_files=true")
    assert response.status_code == 204, response.text
    assert not directory.exists()
    assert not kernel.exists()
    assert not (root / "projects" / project["kind"] / f".delete-{project['id']}").exists()
    assert (root / other["directory"] / "research.ipynb").is_file()
    assert kernel_directory(other["id"]).is_dir()
    assert not report.parent.exists()
    assert client.get(f"/api/v1/reports/{run_id}/report/run.json").status_code == 404
    assert client.get(f"/api/v1/reports/{run_id}/report/result.parquet").status_code == 404
    with Session(engine) as session:
        assert session.get(Project, UUID(project["id"])) is None
        assert session.get(ProjectVersion, run_id) is None
        assert session.get(ProjectDeletion, UUID(project["id"])) is None
    assert client.get(url).status_code == 404
    assert client.delete(url + "?delete_files=true").status_code == 404
    assert client.delete(url).status_code == 404
    assert len(idle_jupyter) == 8


def test_delete_false_retains_workspace_but_removes_live_identity(projects_client, idle_jupyter):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    url = f"/api/v1/projects/{project['id']}"
    assert client.delete(url + "?delete_files=false").status_code == 204
    assert (root / project["directory"]).is_dir()
    assert not kernel_directory(project["id"]).exists()
    with Session(engine) as session:
        assert session.get(Project, UUID(project["id"])) is None
    assert client.delete(url + "?delete_files=true").status_code == 404
    assert (root / project["directory"]).is_dir()
    response = client.post("/api/v1/projects", json={"name": project["name"], "kind": project["kind"],
                                                   "scheme_version": "v1.2.0", "algo_version": "v1.2.0"})
    assert response.status_code == 409 and "目录" in response.json()["detail"]


@pytest.mark.parametrize("phase", ["building", "queued", "running"])
def test_delete_files_rejects_inflight_versions(projects_client, idle_jupyter, phase):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    with Session(engine) as session:
        session.add(ProjectVersion(project_id=UUID(project["id"]), number=1,
                                   package_version="1.2.1", status=phase))
        session.commit()
    response = client.delete(f"/api/v1/projects/{project['id']}?delete_files=true")
    assert response.status_code == 409
    assert (root / project["directory"]).is_dir()
    assert kernel_directory(project["id"]).is_dir()
    assert idle_jupyter == []


@pytest.mark.parametrize("phase", ["failed", "submit_failed"])
def test_delete_files_accepts_finished_versions(projects_client, idle_jupyter, phase):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    with Session(engine) as session:
        session.add(ProjectVersion(project_id=UUID(project["id"]), number=1,
                                   package_version="1.2.1", status=phase))
        session.commit()
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files=true").status_code == 204
    assert not (root / project["directory"]).exists()


@pytest.mark.parametrize("invalid", ["missing", "malformed", "foreign", "kernel"])
def test_delete_files_rejects_unowned_directory(projects_client, idle_jupyter, invalid):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    directory = root / project["directory"]
    marker = directory / ".solo"
    if invalid == "missing":
        marker.unlink()
    elif invalid == "malformed":
        marker.write_text("not json")
    elif invalid == "foreign":
        metadata = json.loads(marker.read_text())
        metadata["project_id"] = str(uuid4())
        marker.write_text(json.dumps(metadata))
    else:
        kernel = kernel_directory(project["id"]) / "kernel.json"
        kernel.write_text(json.dumps({"argv": ["/another/project/.venv/bin/python"]}))
    response = client.delete(f"/api/v1/projects/{project['id']}?delete_files=true")
    assert response.status_code == 409
    assert directory.is_dir()
    assert idle_jupyter == []
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 200


@pytest.mark.parametrize("usage", ["kernel", "project_session", "renamed_session", "unavailable", "malformed", "busy", "noncanonical_path"])
def test_delete_files_checks_jupyter_usage(projects_client, monkeypatch, usage):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    monkeypatch.setattr(SoloSettings, "JUPYTER_TOKEN", "private-token")
    monkeypatch.setattr(SoloSettings, "JUPYTER_API_URL", "http://jupyter.internal/base")

    def get(url, headers, *, params=None, method="GET", body=None):
        assert headers == {"Authorization": "token private-token"}
        if method == "POST":
            assert body == {"project_id": project["id"], "operation": "delete", "lease": body["lease"]}
            assert str(UUID(body["lease"])) == body["lease"]
            return SimpleNamespace(status_code=409 if usage == "busy" else 200,
                                   json=lambda: {"project_id": project["id"], "lease": body["lease"]})
        if method == "DELETE":
            return SimpleNamespace(status_code=204)
        assert url.startswith("/api/")
        sessions, kernels = [], []
        kernel = {"id": "kernel-id", "name": "python3", "execution_state": "busy"}
        if usage == "kernel":
            kernels = [{**kernel, "name": f"solo-{project['id']}"}]
        elif usage == "project_session":
            kernels = [kernel]
            sessions = [{"id": "session-id", "path": project["directory"] + "/research.ipynb", "kernel": kernel}]
        elif usage == "renamed_session":
            kernel = {**kernel, "name": f"solo-{project['id']}"}
            kernels = [kernel]
            sessions = [{"id": "session-id", "path": "elsewhere/notebook.ipynb", "kernel": kernel}]
        elif usage == "noncanonical_path":
            sessions = [{"id": "session-id", "path": "projects/factor/other/../cleanup/research.ipynb", "kernel": kernel}]
        payload = sessions if url.endswith("sessions") else kernels
        if usage == "malformed":
            payload = {"unexpected": "object"}
        return SimpleNamespace(status_code=401 if usage == "unavailable" else 200, json=lambda: payload)

    monkeypatch.setattr(deletion, "jupyter_request", get)
    response = client.delete(f"/api/v1/projects/{project['id']}?delete_files=true")
    assert response.status_code == (503 if usage in {"unavailable", "malformed", "noncanonical_path"} else 409)
    assert (root / project["directory"]).is_dir()
    assert kernel_directory(project["id"]).is_dir()
    assert "private-token" not in response.text


def test_delete_files_ignores_similarly_named_project_session(projects_client, monkeypatch):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    def get(url, headers, *, params=None, method="GET", body=None):
        if method == "POST":
            return SimpleNamespace(status_code=200, json=lambda: {"project_id": project["id"], "lease": body["lease"]})
        if method == "DELETE":
            assert url == "/solo/project/operations"
            return SimpleNamespace(status_code=204)
        kernel = {"id": "other-kernel", "name": "python3", "execution_state": "busy"}
        payload = [{"id": "other-session", "path": project["directory"] + "-other/research.ipynb", "kernel": kernel}] if url.endswith("sessions") else [kernel]
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(deletion, "jupyter_request", get)
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files=true").status_code == 204
    assert not (root / project["directory"]).exists()


def test_delete_files_rolls_back_staging_if_kernel_starts(projects_client, monkeypatch):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    checks = 0
    def get(url, headers, *, params=None, method="GET", body=None):
        nonlocal checks
        if method == "POST":
            return SimpleNamespace(status_code=200, json=lambda: {"project_id": project["id"], "lease": body["lease"]})
        if method == "DELETE":
            return SimpleNamespace(status_code=204)
        if url.endswith("sessions"):
            checks += 1
        payload = [{"id": "late-kernel", "name": f"solo-{project['id']}", "execution_state": "busy"}] if checks > 2 and url.endswith("kernels") else []
        return SimpleNamespace(status_code=200, json=lambda: payload)
    monkeypatch.setattr(deletion, "jupyter_request", get)
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files=true").status_code == 409
    assert (root / project["directory"] / ".solo").is_file()
    assert not (root / "projects" / project["kind"] / f".delete-{project['id']}").exists()
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 200


def test_delete_files_commit_failure_restores_workspace(projects_client, idle_jupyter, monkeypatch):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    with Session(engine) as session:
        record = session.get(Project, UUID(project["id"]))
        original_commit = session.commit
        commits = 0
        def fail():
            nonlocal commits
            commits += 1
            if commits == 2:
                raise RuntimeError("commit failed")
            return original_commit()
        monkeypatch.setattr(session, "commit", fail)
        with pytest.raises(RuntimeError, match="commit failed"):
            deletion.delete_project(record.id, session)
    assert (root / project["directory"] / ".solo").is_file()
    assert kernel_directory(project["id"]).is_dir()
    assert not (root / "projects" / project["kind"] / f".delete-{project['id']}").exists()
    assert client.get(f"/api/v1/projects/{project['id']}").status_code == 200


def test_delete_files_cleanup_failure_is_retryable(projects_client, idle_jupyter, monkeypatch):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    original = deletion.shutil.rmtree
    def fail(path, *args, **kwargs):
        raise PermissionError("directory busy")
    monkeypatch.setattr(deletion.shutil, "rmtree", fail)
    url = f"/api/v1/projects/{project['id']}?delete_files=true"
    response = client.delete(url)
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "project_cleanup_incomplete"
    assert response.json()["detail"]["deleted"] is True
    assert not (root / project["directory"]).exists()
    stage = root / "projects" / project["kind"] / f".delete-{project['id']}"
    assert (stage / "metadata.json").is_file()
    assert (stage / "project" / "research.ipynb").is_file()
    assert not kernel_directory(project["id"]).exists()
    assert (kernel_directory(project["id"]).parent / f".delete-{project['id']}" / "kernel").is_dir()
    monkeypatch.setattr(deletion.shutil, "rmtree", original)
    assert client.delete(url).status_code == 204
    assert not stage.exists()
    assert not kernel_directory(project["id"]).exists()


@pytest.mark.parametrize("consumer_kind", ["project", "strategy"])
def test_delete_files_does_not_block_frozen_active_consumer(projects_client, idle_jupyter, consumer_kind):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    if consumer_kind == "project":
        consumer = create_cleanup_project(client, name="consumer", kind="model")
        (root / consumer["directory"] / "uv.lock").write_text(
            f'[[package]]\nname="factor-{UUID(project["id"]).hex[:4]}"\nversion="1.2.1"\n',
        )
    with Session(engine) as session:
        if consumer_kind == "project":
            session.add(ProjectVersion(project_id=UUID(consumer["id"]), number=1,
                                       package_version="1.2.1", status="building"))
        else:
            session.add(Strategy(name="consumer", status="building", forms={},
                                 components={"factor": {"project_id": project["id"]}}))
        session.commit()
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files=true").status_code == 204
    assert not (root / project["directory"]).exists()
    assert idle_jupyter
    with Session(engine) as session:
        if consumer_kind == "project":
            assert session.get(Project, UUID(consumer["id"])) is not None
            assert session.scalar(select(ProjectVersion)).status == "building"
        else:
            assert session.scalar(select(Strategy)).status == "building"


@pytest.mark.parametrize("location", ["project", "kind", "marker", "kernel"])
def test_delete_files_rejects_symlink_paths(projects_client, idle_jupyter, location):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    directory = root / project["directory"]
    if location == "marker":
        original = directory / ".solo"
        target = root / "outside-marker.json"
    elif location == "kernel":
        original = kernel_directory(project["id"])
        target = root / "outside-kernel"
    elif location == "kind":
        original = directory.parent
        target = root / "outside-kind"
    else:
        original = directory
        target = root / "outside-project"
    original.rename(target)
    try:
        original.symlink_to(target, target_is_directory=location != "marker")
    except OSError:
        target.rename(original)
        pytest.skip("系统不允许创建测试符号链接")
    response = client.delete(f"/api/v1/projects/{project['id']}?delete_files=true")
    assert response.status_code == 409
    assert target.exists()
    assert original.is_symlink()
    assert idle_jupyter == []


def test_delete_files_does_not_follow_nested_links(projects_client, idle_jupyter):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    target = root / "unrelated-source.py"
    target.write_text("keep")
    link = root / project["directory"] / "linked-source.py"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("系统不允许创建测试符号链接")
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files=true").status_code == 204
    assert target.read_text() == "keep"


def test_delete_files_handles_missing_workspace(projects_client, idle_jupyter):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    deletion.shutil.rmtree(root / project["directory"])
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files=true").status_code == 204
    assert not kernel_directory(project["id"]).exists()


def test_delete_files_can_retry_partial_kernel_cleanup(projects_client, idle_jupyter, monkeypatch):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    original = deletion.shutil.rmtree
    kernel = kernel_directory(project["id"])
    staged_kernel = kernel.parent / f".delete-{project['id']}" / "kernel"
    def fail(path, *args, **kwargs):
        if path == staged_kernel:
            (staged_kernel / "kernel.json").unlink()
            raise PermissionError("partial kernel cleanup")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(deletion.shutil, "rmtree", fail)
    url = f"/api/v1/projects/{project['id']}?delete_files=true"
    assert client.delete(url).status_code == 503
    assert not kernel.exists()
    assert staged_kernel.is_dir()
    assert not (staged_kernel / "kernel.json").exists()
    monkeypatch.setattr(deletion.shutil, "rmtree", original)
    assert client.delete(url).status_code == 204
    assert not kernel.exists()
    assert not (root / "projects" / project["kind"] / f".delete-{project['id']}").exists()


def test_delete_files_can_retry_final_staging_removal_failure(projects_client, idle_jupyter, monkeypatch):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    stage = root / "projects" / project["kind"] / f".delete-{project['id']}"
    original = Path.rmdir
    def fail(path):
        if path == stage:
            raise PermissionError("final removal failed")
        return original(path)
    monkeypatch.setattr(Path, "rmdir", fail)
    url = f"/api/v1/projects/{project['id']}?delete_files=true"
    assert client.delete(url).status_code == 503
    assert stage.is_dir()
    assert list(stage.iterdir()) == []
    assert not kernel_directory(project["id"]).exists()
    monkeypatch.setattr(Path, "rmdir", original)
    assert client.delete(url).status_code == 204
    assert not stage.exists()


def test_jupyter_request_keeps_token_and_ignores_implicit_auth(monkeypatch):
    prepared = []
    monkeypatch.setattr(SoloSettings, "JUPYTER_API_URL", "http://jupyter.invalid/base")
    monkeypatch.setattr(deletion.requests.sessions, "get_netrc_auth", lambda *args: ("other-user", "other-password"))
    def send(self, request, **kwargs):
        prepared.append((self.trust_env, request, kwargs))
        response = deletion.requests.Response()
        response.status_code = 200
        response._content = b"[]"
        return response
    monkeypatch.setattr(deletion.requests.Session, "send", send)
    assert deletion.jupyter_request("/api/kernels", {"Authorization": "token private-token"}).status_code == 200
    trust_env, request, kwargs = prepared[0]
    assert trust_env is False
    assert request.url == "http://jupyter.invalid/base/api/kernels"
    assert request.headers["Authorization"] == "token private-token"
    assert kwargs["timeout"] == (3, 10)
    assert kwargs["allow_redirects"] is False


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
