"""Upgrade lifecycles use temporary workspaces, SQLite and fake uv/Jupyter."""

import json
import os
import shutil
import tomllib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import requests
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.projects import deletion, templates, upgrade, upgrade_environment, views
from core.apps.projects.environment import EnvironmentError, kernel_directory
from core.apps.projects.models import Project, ProjectDeletion, ProjectVersion
from core.apps.projects.schemas import ProjectUpgrade, TemplateVersion
from test_projects import create_cleanup_project, projects_client  # noqa: F401 -- imported pytest fixture

TARGET = {"scheme_version": "v1.6.0", "algo_version": "v1.6.0"}
SCHEME = TemplateVersion(tag="v1.6.0", commit="c" * 40, version="1.6.0")
ALGO = TemplateVersion(tag="v1.6.0", commit="d" * 40, version="1.6.0")
TEMPLATE = b'''[project]
name = "factor"
version = "1.6.0"
dependencies = ["scheme>=1.6.0,<1.7.0", "pydantic>=2,<3"]
[dependency-groups]
dev = ["ipykernel>=6"]
'''
FILES = ("pyproject.toml", "uv.lock", ".solo")


def operations_response(status_code, payload):
    response = requests.Response()
    response.status_code = status_code
    response._content = json.dumps(payload).encode()
    return response


class Resources:
    def __init__(self, project_id):
        self.project_id = project_id
        self.busy = False
        self.calls = []
        self.sessions = []
        self.kernels = []

    def request(self, path, headers, *, method="GET", **kwargs):
        self.calls.append((method, path))
        if path == "/solo/project/operations":
            assert method == "GET" and kwargs == {"params": {"project_id": self.project_id}}
            return operations_response(200, {"project_id": self.project_id, "busy": self.busy})
        assert path.startswith("/api/")
        if method == "GET":
            assert path in {"/api/sessions", "/api/kernels"}
            return SimpleNamespace(status_code=200, json=lambda: self.sessions if path.endswith("sessions") else self.kernels)
        assert method == "DELETE"
        identifier = path.rsplit("/", 1)[-1]
        if path.startswith("/api/sessions/"):
            self.sessions = [item for item in self.sessions if item["id"] != identifier]
        else:
            assert path.startswith("/api/kernels/")
            self.kernels = [item for item in self.kernels if item["id"] != identifier]
        return SimpleNamespace(status_code=204)


@pytest.fixture
def workspace(projects_client, monkeypatch):  # noqa: F811 -- pytest injects the imported fixture
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    directory = root / project["directory"]
    (directory / "research.ipynb").write_bytes(b'{ "cells": [{"cell_type":"code", "source":["USER=123\\n"]}], "metadata": {} }\n')
    source = next((directory / "src").rglob("__init__.py"))
    source.write_bytes(b"# user source, never migrate\r\nPARAMETERS = {'mine': 1}\r\n")
    marker = json.loads((directory / ".solo").read_bytes())
    marker["custom"] = {"keep": [1, 2]}
    (directory / ".solo").write_text(json.dumps(marker), encoding="utf-8")
    monkeypatch.setattr(templates, "select_scheme", lambda tag: SCHEME)
    monkeypatch.setattr(templates, "list_versions", lambda *args: [ALGO])

    def template_file(repository, commit, path):
        assert repository == SoloSettings.TEMPLATE_REPOSITORY
        assert commit == ALGO.commit and path == "factor/pyproject.toml"
        return TEMPLATE

    monkeypatch.setattr(templates, "git_file", template_file)
    resources = Resources(project["id"])
    monkeypatch.setattr(deletion, "jupyter_request", resources.request)
    installs = []

    def install(path):
        assert path == directory
        data = tomllib.loads((path / "pyproject.toml").read_text(encoding="utf-8"))
        version = data["project"]["version"]
        installs.append(version)
        (path / ".venv").mkdir(exist_ok=True)
        (path / ".venv" / "installed-version").write_text(version)
        if version == "1.6.0":
            (path / "uv.lock").write_text(
                f'[[package]]\nname="{project["packageName"]}"\nversion="{version}"\nsource={{editable="."}}\n'
                f'[[package]]\nname="scheme"\nversion="{version}"\n', encoding="utf-8",
            )

    monkeypatch.setattr(upgrade_environment, "install_environment", install)
    return SimpleNamespace(client=client, root=root, engine=engine, project=project, directory=directory,
                           source=source, resources=resources, installs=installs, install=install,
                           url=f'/api/v1/projects/{project["id"]}/upgrade')


def files(directory):
    return {name: (directory / name).read_bytes() for name in FILES}


def assert_original(w, before):
    assert files(w.directory) == before
    assert not list(w.directory.parent.glob(".upgrade-*"))
    with Session(w.engine) as session:
        project = session.get(Project, UUID(w.project["id"]))
        assert project.scheme_version == project.template_tag == "v1.2.0"


def test_upgrade_preserves_source_notebook_kernel_custom_metadata_and_history(workspace):
    w = workspace
    source, notebook = w.source.read_bytes(), (w.directory / "research.ipynb").read_bytes()
    kernel = kernel_directory(w.project["id"]) / "kernel.json"
    kernel_before = kernel.read_bytes()
    with Session(w.engine) as session:
        version = ProjectVersion(project_id=UUID(w.project["id"]), number=1, package_version="1.2.1",
                                 status="success", parameters={"untouched": 123}, finished_at=datetime.now(UTC))
        session.add(version)
        session.commit()
        version_id = version.id
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["id"] == w.project["id"] and data["versions"][0]["id"] == str(version_id)
    assert data["schemeVersion"] == data["algoVersion"] == "v1.6.0"
    assert data["schemeCommit"] == SCHEME.commit and data["algoCommit"] == ALGO.commit
    assert not data["retired"]
    assert w.source.read_bytes() == source and (w.directory / "research.ipynb").read_bytes() == notebook
    assert kernel.read_bytes() == kernel_before
    metadata = json.loads((w.directory / ".solo").read_bytes())
    assert metadata["custom"] == {"keep": [1, 2]}
    assert metadata["source_template"] == {
        "algo_version": "v1.2.0", "algo_commit": "a" * 40, "template_package_version": "1.2.0",
    }
    assert metadata["algo_version"] == metadata["scheme_version"] == "v1.6.0"
    assert w.installs == ["1.6.0"]
    assert not list(w.directory.parent.glob(".upgrade-*"))
    with Session(w.engine) as session:
        saved = session.get(Project, UUID(w.project["id"]))
        assert saved.template_package_version == "1.6.0" and saved.template_commit == ALGO.commit
        assert session.get(ProjectVersion, version_id).parameters == {"untouched": 123}
        views.write_project_metadata(saved, w.directory)
    assert json.loads((w.directory / ".solo").read_bytes()) == metadata


def test_same_target_returns_without_install_or_file_changes(workspace, monkeypatch):
    w = workspace
    assert w.client.post(w.url, json=TARGET).status_code == 200
    before = files(w.directory)
    w.resources.calls.clear()

    def stop(*args, **kwargs):
        pytest.fail("No-op upgrades must not close Jupyter kernels")

    monkeypatch.setattr(deletion, "stop_jupyter", stop)
    w.resources.busy = True
    assert w.client.post(w.url, json=TARGET).status_code == 200
    assert files(w.directory) == before and w.installs == ["1.6.0"]
    assert w.resources.calls == []


def test_jupyter_operation_started_first_blocks_upgrade_before_kernel_or_file_changes(workspace):
    w = workspace
    before = files(w.directory)
    w.resources.busy = True
    check = f'/api/v1/version-policy/projects/{w.project["id"]}/check'
    assert w.client.post(check).json() == {"allowed": True}
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 409, response.text
    assert w.resources.calls == [("GET", "/solo/project/operations")]
    assert not w.installs
    assert_original(w, before)
    w.resources.busy = False
    assert w.client.post(w.url, json=TARGET).status_code == 200


def test_upgrade_started_first_rejects_jupyter_admission_before_changing_files(workspace, monkeypatch):
    w = workspace
    before = files(w.directory)
    identifier = UUID(w.project["id"])
    entered, resume = Event(), Event()
    stop = deletion.stop_jupyter

    def stop_jupyter(*args, **kwargs):
        entered.set()
        assert resume.wait(10), "test did not release upgrade"
        stop(*args, **kwargs)

    def run_upgrade():
        with Session(w.engine, expire_on_commit=False) as session:
            return upgrade.upgrade_project(identifier, ProjectUpgrade(**TARGET), session)

    monkeypatch.setattr(deletion, "stop_jupyter", stop_jupyter)
    check = f"/api/v1/version-policy/projects/{identifier}/check"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_upgrade)
        try:
            assert entered.wait(10), "upgrade did not reach kernel shutdown"
            assert files(w.directory) == before
            assert w.resources.calls == [("GET", "/solo/project/operations")]
            w.resources.busy = True
            response = w.client.post(check)
            assert response.status_code == 409, response.text
            assert response.json()["detail"] == {
                "code": "project_busy", "reason": "项目正在升级、修改或删除，请稍后重试",
            }
            assert not w.installs
        finally:
            w.resources.busy = False
            resume.set()
        assert future.result(timeout=10).scheme_version == "v1.6.0"
    assert w.installs == ["1.6.0"]
    assert w.client.post(check).json() == {"allowed": True}


@pytest.mark.parametrize("failure", ["request", "status"])
def test_upgrade_rejects_unconfirmed_jupyter_operation_state(workspace, monkeypatch, failure):
    w = workspace
    before = files(w.directory)
    monkeypatch.setattr(SoloSettings, "JUPYTER_TOKEN", "private-token")

    def request(path, headers, *, params):
        assert path == "/solo/project/operations" and params == {"project_id": w.project["id"]}
        assert headers == {"Authorization": "token private-token"}
        if failure == "request":
            raise requests.ConnectionError("unavailable private-token")
        return operations_response(503, {"message": "unavailable"})

    monkeypatch.setattr(deletion, "jupyter_request", request)
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 503, response.text
    assert "private-token" not in response.text
    assert not w.installs
    assert_original(w, before)


def test_uv_failure_restores_files_and_syncs_original_environment(workspace, monkeypatch):
    w = workspace
    before = files(w.directory)

    def install(path):
        if not w.installs:
            w.install(path)
            raise EnvironmentError("No solution found: injected uv output")
        assert files(path) == before
        w.install(path)

    monkeypatch.setattr(upgrade_environment, "install_environment", install)
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 502, response.text
    assert "No solution found: injected uv output" in response.json()["detail"]
    assert w.installs == ["1.6.0", "1.2.0"]
    assert (w.directory / ".venv" / "installed-version").read_text() == "1.2.0"
    assert_original(w, before)


def test_commit_failure_restores_metadata_and_environment_and_reraises(workspace, monkeypatch):
    w = workspace
    before = files(w.directory)
    with Session(w.engine) as session:
        original_rollback = session.rollback
        events = []

        def commit():
            assert json.loads((w.directory / ".solo").read_bytes())["algo_version"] == "v1.6.0"
            events.append("commit")
            raise RuntimeError("injected commit failure")

        def install(path):
            assert session.in_transaction()
            if w.installs:
                assert files(path) == before
            w.install(path)
            events.append(w.installs[-1])

        def rollback():
            assert events == ["1.6.0", "commit", "1.2.0"]
            assert files(w.directory) == before
            assert (w.directory / ".venv" / "installed-version").read_text() == "1.2.0"
            events.append("rollback")
            original_rollback()

        monkeypatch.setattr(session, "commit", commit)
        monkeypatch.setattr(session, "rollback", rollback)
        monkeypatch.setattr(upgrade_environment, "install_environment", install)
        with pytest.raises(RuntimeError, match="injected commit failure"):
            upgrade.upgrade_project(UUID(w.project["id"]), ProjectUpgrade(**TARGET), session)
        assert events == ["1.6.0", "commit", "1.2.0", "rollback"]
    assert w.installs == ["1.6.0", "1.2.0"]
    assert_original(w, before)


@pytest.mark.parametrize("failure", ["restore-files", "restore-environment"])
def test_rollback_runs_even_when_upgrade_recovery_fails(workspace, monkeypatch, failure):
    w = workspace
    before = files(w.directory)
    write_bytes = type(w.directory).write_bytes
    with Session(w.engine) as session:
        original_rollback = session.rollback
        events = []

        def write(path, content):
            if failure == "restore-files" and path == w.directory / "pyproject.toml" and content == before[path.name]:
                assert session.in_transaction()
                events.append("restore-files")
                raise OSError("injected recovery failure")
            return write_bytes(path, content)

        def install(path):
            assert session.in_transaction()
            if w.installs:
                assert files(path) == before
                events.append("restore-environment")
                raise EnvironmentError("injected recovery failure")
            w.install(path)
            raise RuntimeError("injected install failure")

        def rollback():
            assert events == [failure]
            events.append("rollback")
            original_rollback()

        monkeypatch.setattr(type(w.directory), "write_bytes", write)
        monkeypatch.setattr(upgrade_environment, "install_environment", install)
        monkeypatch.setattr(session, "rollback", rollback)
        with pytest.raises(OSError if failure == "restore-files" else upgrade.HTTPException, match="injected recovery failure"):
            upgrade.upgrade_project(UUID(w.project["id"]), ProjectUpgrade(**TARGET), session)
        assert events == [failure, "rollback"]
        assert not session.in_transaction()
        assert not deletion.project_lifecycle_busy(UUID(w.project["id"]))


def test_upgrade_allows_downgrade_to_active_target(workspace):
    w = workspace
    with Session(w.engine) as session:
        project = session.get(Project, UUID(w.project["id"]))
        project.scheme_version = project.template_tag = "v1.7.0"
        project.template_package_version = "1.7.0"
        session.commit()
    path = w.directory / "pyproject.toml"
    path.write_bytes(path.read_bytes().replace(b"1.2.0", b"1.7.0"))
    assert w.client.post(w.url, json=TARGET).status_code == 200
    assert w.installs == ["1.6.0"]


def test_upgrade_does_not_require_existing_venv(workspace):
    w = workspace
    shutil.rmtree(w.directory / ".venv")
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 200, response.text
    assert (w.directory / ".venv" / "installed-version").read_text() == "1.6.0"


def test_upgrade_preserves_relative_absolute_dangling_and_cyclic_source_links(workspace):
    w = workspace
    outside = w.root / "external.txt"
    outside.write_bytes(b"external data")
    links = {"source-file": (str(w.source), False), "source-directory": ("src", True),
             "relative-file": (os.path.relpath(w.source, w.directory), False),
             "chain": ("source-directory", True), "external": (str(outside), False),
             "dangling": ("missing", False), "cycle": (".", True),
             "file-cycle-a": ("file-cycle-b", False), "file-cycle-b": ("file-cycle-a", False)}
    try:
        for name, (target, directory) in links.items():
            (w.directory / name).symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("系统不允许创建符号链接")
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 200, response.text
    for name, (target, _) in links.items():
        assert (w.directory / name).is_symlink() and os.readlink(w.directory / name) == target
    assert outside.read_bytes() == b"external data"


def test_upgrade_leaves_absolute_local_dependencies_to_uv(workspace):
    w = workspace
    path = w.directory / "pyproject.toml"
    with path.open("a", encoding="utf-8") as stream:
        stream.write(f'\n[tool.uv.sources.local-package]\npath = {json.dumps(str(w.root / "local-package"))}\n')
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 200, response.text
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    assert config["tool"]["uv"]["sources"]["local-package"]["path"] == str(w.root / "local-package")


@pytest.mark.parametrize("phase", ["building", "queued", "running"])
def test_running_research_blocks_upgrade_before_jupyter_or_install(workspace, phase):
    w = workspace
    with Session(w.engine) as session:
        session.add(ProjectVersion(project_id=UUID(w.project["id"]), number=1, package_version="1.2.1", status=phase))
        session.commit()
    assert w.client.post(w.url, json=TARGET).status_code == 409
    assert not w.resources.calls and not w.installs


@pytest.mark.parametrize("usage", ["idle-own", "idle-subtree", "busy", "shared"])
def test_upgrade_shuts_down_only_idle_owned_jupyter_resources(workspace, usage):
    w = workspace
    own = {"id": "own-kernel", "name": f'solo-{w.project["id"]}', "execution_state": "busy" if usage == "busy" else "idle"}
    other = {"id": "other-kernel", "name": "python3", "execution_state": "busy"}
    foreign = {"id": "foreign", "path": w.project["directory"] + "-other/research.ipynb", "kernel": other}
    if usage in {"idle-subtree", "shared"}:
        own["name"] = "python3"
    w.resources.kernels = [own, other]
    w.resources.sessions = [{"id": "own", "path": w.project["directory"] + "/research.ipynb", "kernel": own}, foreign]
    if usage == "shared":
        w.resources.sessions.append({"id": "shared", "path": "projects/model/other/research.ipynb", "kernel": own})
    response = w.client.post(w.url, json=TARGET)
    deleted = [path for method, path in w.resources.calls if method == "DELETE"]
    if usage.startswith("idle"):
        assert response.status_code == 200, response.text
        assert deleted == ["/api/sessions/own", "/api/kernels/own-kernel"]
        assert w.resources.sessions == [foreign] and w.resources.kernels == [other]
    else:
        assert response.status_code == 409 and not deleted and not w.installs


@pytest.mark.parametrize("condition", ["missing", "archived", "deleting", "lifecycle-busy"])
def test_upgrade_rejects_unavailable_project(workspace, condition):
    w = workspace
    identifier = UUID(w.project["id"])
    with Session(w.engine) as session:
        if condition == "archived":
            session.get(Project, identifier).archived = True
            session.commit()
        elif condition == "deleting":
            session.add(ProjectDeletion(id=identifier, project_name=w.project["name"], kind="factor"))
            session.commit()
        elif condition == "missing":
            w.url = f"/api/v1/projects/{uuid4()}/upgrade"
        if condition == "lifecycle-busy":
            with deletion.project_lifecycle_guard(identifier, session):
                assert w.client.post(w.url, json=TARGET).status_code == 409
        else:
            assert w.client.post(w.url, json=TARGET).status_code == (404 if condition in {"missing", "archived"} else 409)
    assert not w.resources.calls and not w.installs


@pytest.mark.parametrize("invalid", ["missing-template", "version-mismatch", "series", "range", "retired"])
def test_invalid_target_never_changes_files(workspace, monkeypatch, invalid):
    w = workspace
    before = files(w.directory)
    stop_calls = []
    monkeypatch.setattr(deletion, "stop_jupyter", lambda *args, **kwargs: stop_calls.append(args))
    if invalid == "missing-template":
        monkeypatch.setattr(templates, "list_versions", lambda *args: [])
    elif invalid == "version-mismatch":
        monkeypatch.setattr(templates, "git_file", lambda *args: TEMPLATE.replace(b'"1.6.0"', b'"1.6.1"', 1))
    elif invalid == "series":
        monkeypatch.setattr(templates, "list_versions", lambda *args: [ALGO.model_copy(update={"version": "1.5.0"})])
        monkeypatch.setattr(templates, "git_file", lambda *args: TEMPLATE.replace(b'"1.6.0"', b'"1.5.0"', 1))
    elif invalid == "range":
        monkeypatch.setattr(templates, "git_file", lambda *args: TEMPLATE.replace(b">=1.6.0", b">=1.6.1"))
    else:
        def retired(*args, **kwargs):
            raise policy.VersionPolicyError("target retired")
        monkeypatch.setattr(policy, "require_release", retired)
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 422, response.text
    assert_original(w, before)
    assert not w.installs and not stop_calls


@pytest.mark.parametrize("invalid", ["solo-identity", "package-identity", "directory-escape", "config-escape"])
def test_upgrade_never_overwrites_unowned_or_out_of_boundary_files(workspace, monkeypatch, invalid):
    w = workspace
    if invalid == "solo-identity":
        path = w.directory / ".solo"
        data = json.loads(path.read_bytes())
        data["project_id"] = str(uuid4())
        path.write_text(json.dumps(data), encoding="utf-8")
    elif invalid == "package-identity":
        path = w.directory / "pyproject.toml"
        path.write_bytes(path.read_bytes().replace(w.project["packageName"].encode(), b"factor-other"))
    else:
        original = type(w.directory).resolve
        outside = w.root.parent / "outside"
        redirected = w.directory if invalid == "directory-escape" else w.directory / "pyproject.toml"
        monkeypatch.setattr(type(w.directory), "resolve", lambda path, **kwargs:
                            outside if path == redirected else original(path, **kwargs))
    before = files(w.directory)
    response = w.client.post(w.url, json=TARGET)
    assert response.status_code == 409, response.text
    assert files(w.directory) == before and not w.installs


def test_retired_original_release_can_upgrade(workspace, monkeypatch):
    original = policy.require_release

    def require(kind, **source):
        if source.get("commit") in {"a" * 40, "b" * 40}:
            raise policy.VersionPolicyError("old retired")
        original(kind, **source)

    monkeypatch.setattr(policy, "require_release", require)
    assert workspace.client.post(workspace.url, json=TARGET).status_code == 200
