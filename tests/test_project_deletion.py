"""True deletion safety using temporary files, SQLite, and API-shaped resource fakes."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from uuid import UUID, uuid4
from io import BytesIO
from zipfile import ZipFile

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import DolphinSchedulerSettings
from core.apps.artifacts.models import ResearchArtifact
from core.apps.artifacts.service import artifact_snapshot
from core.apps.projects import deletion, views
from core.apps.projects.environment import EnvironmentError, kernel_directory
from core.apps.projects.models import Project, ProjectDeletion, ProjectVersion
from core.apps.projects.schemas import ProjectEdit
from core.apps.strategies.models import Strategy
from core.scheduler.client import DolphinSchedulerClient, DolphinSchedulerError, instance_input_file
from test_projects import create_cleanup_project
from test_projects import projects_client as projects_client


class JupyterResources:
    def __init__(self):
        self.sessions = []
        self.kernels = []
        self.calls = []
        self.lease = None
        self.lease_project = None
        self.source_busy = False
        self.lose_acquire_response = False
        self.fail_release = False
        self.lose_release_response = False

    def request(self, path, headers, *, params=None, method="GET", body=None):
        self.calls.append((method, path, body, headers))
        if path == "/solo/project/operations":
            if method == "POST":
                assert body["operation"] == "delete" and isinstance(body["lease"], str) and body["lease"]
                if self.source_busy or (self.lease is not None and
                        (self.lease != body["lease"] or self.lease_project != body["project_id"])):
                    return SimpleNamespace(status_code=409)
                self.lease, self.lease_project = body["lease"], body["project_id"]
                if self.lose_acquire_response:
                    self.lose_acquire_response = False
                    raise deletion.requests.ConnectionError("response lost after successful acquire")
                return SimpleNamespace(status_code=200, json=lambda: {"project_id": body["project_id"], "lease": body["lease"]})
            assert method == "DELETE"
            if self.lease is not None and (self.lease != body["lease"] or self.lease_project != body["project_id"]):
                return SimpleNamespace(status_code=409)
            if self.fail_release:
                return SimpleNamespace(status_code=503)
            self.lease = self.lease_project = None
            if self.lose_release_response:
                self.lose_release_response = False
                raise deletion.requests.ConnectionError("response lost after successful release")
            return SimpleNamespace(status_code=204)
        if method == "GET":
            assert path in {"/api/kernels", "/api/sessions"}
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
def resources(monkeypatch):
    result = JupyterResources()
    monkeypatch.setattr(deletion, "jupyter_request", result.request)
    return result


def add_version(engine, root, project, *, workflow_id=None, status="success"):
    run_id = uuid4()
    run = root / "runs" / str(run_id)
    (run / "report").mkdir(parents=True)
    (run / "report" / "run.json").write_text('{"status":"success"}')
    (run / "input.json").write_text(json.dumps({"kind": project["kind"]}))
    with Session(engine) as session:
        session.add(ProjectVersion(id=run_id, project_id=UUID(project["id"]), number=1,
                                   package_version="1.2.1", status=status, workflow_id=workflow_id))
        session.commit()
    return run_id, run


def test_retry_old_uuid_never_touches_new_same_name_workspace_or_sessions(projects_client, resources, monkeypatch):
    client, root, engine = projects_client
    old = create_cleanup_project(client)
    run_id, run = add_version(engine, root, old)
    original = deletion.shutil.rmtree
    failed = root / "runs" / f".delete-{old['id']}" / str(run_id)

    def fail(path, *args, **kwargs):
        if path == failed:
            raise PermissionError("temporary failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(deletion.shutil, "rmtree", fail)
    url = f"/api/v1/projects/{old['id']}"
    response = client.delete(url)
    assert response.status_code == 503
    assert response.json()["detail"] == {"code": "project_cleanup_incomplete", "deleted": True,
                                         "id": old["id"], "reason": "项目已删除，但资源清理未完成，请按原项目 ID 重试"}
    with Session(engine) as session:
        assert session.get(Project, UUID(old["id"])) is None
        assert session.get(ProjectVersion, run_id) is None
        assert session.get(ProjectDeletion, UUID(old["id"])) is not None
    new = create_cleanup_project(client, name=old["name"])
    workspace = root / new["directory"]
    before = {file.relative_to(workspace).as_posix(): file.read_bytes() for file in workspace.rglob("*") if file.is_file()}
    kernel = {"id": "new-kernel", "name": f"solo-{new['id']}", "execution_state": "busy"}
    resources.kernels = [kernel]
    resources.sessions = [{"id": "new-session", "path": new["directory"] + "/research.ipynb", "kernel": kernel}]
    monkeypatch.setattr(deletion.shutil, "rmtree", original)
    assert client.delete(url + "?delete_files=false").status_code == 204  # Retry uses the old committed intent.
    assert resources.kernels == [kernel] and len(resources.sessions) == 1
    assert {file.relative_to(workspace).as_posix(): file.read_bytes() for file in workspace.rglob("*") if file.is_file()} == before
    assert kernel_directory(new["id"]).exists()
    assert client.delete(url).status_code == 404
    assert not run.exists() and not failed.exists()
    with Session(engine) as session:
        assert session.get(ProjectDeletion, UUID(old["id"])) is None
        assert session.get(Project, UUID(new["id"])) is not None


def test_retained_folder_option_only_retains_workspace(projects_client, resources):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    run_id, run = add_version(engine, root, project)
    workspace = root / project["directory"]
    marker = (workspace / ".solo").read_bytes()
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files=false").status_code == 204
    assert workspace.exists() and (workspace / ".solo").read_bytes() == marker
    assert not run.exists() and not kernel_directory(project["id"]).exists()
    with Session(engine) as session:
        assert session.get(Project, UUID(project["id"])) is None
        assert session.get(ProjectVersion, run_id) is None
        assert session.scalar(select(ProjectDeletion)) is None
    blocked = client.post("/api/v1/projects", json={"name": project["name"], "kind": project["kind"],
                                                   "scheme_version": "v1.2.0", "algo_version": "v1.2.0"})
    assert blocked.status_code == 409 and "保留的工作区" in blocked.json()["detail"]
    workspace.rename(workspace.with_name("manual-retained-source"))
    assert create_cleanup_project(client, name=project["name"])["id"] != project["id"]


def artifact(engine, root, project, *, published, content=b"wheel"):
    contents = BytesIO()
    module = project["packageName"].replace("-", "_")
    with ZipFile(contents, "w") as wheel:
        wheel.writestr(f"{module}-1.2.1.dist-info/METADATA", f"Name: {project['packageName']}\nVersion: 1.2.1\n")
        wheel.writestr(f"{module}/__init__.py", content)
    content = contents.getvalue()
    identifier = uuid4()
    filename = module + "-1.2.1-py3-none-any.whl"
    directory = root / "artifacts" / str(identifier)
    directory.mkdir(parents=True)
    (directory / filename).write_bytes(content)
    record = ResearchArtifact(id=identifier, kind=project["kind"], package_name=project["packageName"],
        version="1.2.1", storage_directory=str(identifier), filename=filename, size_bytes=len(content), entry=module + ":Algo",
        scheme_version="1.2.0", sources={}, origin_project_id=UUID(project["id"]), origin_project_name=project["name"],
        published_at=datetime.now(UTC) if published else None, dependencies={})
    with Session(engine, expire_on_commit=False) as session:
        session.add(record)
        session.commit()
    return record, directory / filename


def test_published_and_accepted_artifacts_survive_unreferenced_candidates_gc(projects_client, resources):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    consumer = create_cleanup_project(client, name="consumer", kind="model")
    published, published_wheel = artifact(engine, root, project, published=True)
    accepted, accepted_wheel = artifact(engine, root, project, published=False, content=b"accepted-wheel")
    orphan, orphan_wheel = artifact(engine, root, project, published=False, content=b"orphan-wheel")
    published_bytes, accepted_bytes = published_wheel.read_bytes(), accepted_wheel.read_bytes()
    with Session(engine) as session:
        saved_consumer = session.get(Project, UUID(consumer["id"]))
        saved_consumer.artifact_dependencies = {accepted.package_name: artifact_snapshot(accepted)}
        strategy = Strategy(name="independent", status="running", forms={}, components={"model": {"artifact_id": str(published.id)}},
                            accepted_artifacts={published.package_name: artifact_snapshot(published)})
        session.add(strategy)
        session.commit()
        strategy_id = strategy.id
    independent_run = root / "runs" / str(strategy_id)
    independent_run.mkdir(parents=True)
    (independent_run / "report.txt").write_bytes(b"independent strategy run")
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    assert published_wheel.read_bytes() == published_bytes and accepted_wheel.read_bytes() == accepted_bytes
    assert not orphan_wheel.parent.exists()
    with Session(engine) as session:
        assert session.get(ResearchArtifact, published.id) is not None
        assert session.get(ResearchArtifact, accepted.id) is not None
        assert session.get(ResearchArtifact, orphan.id) is None
        assert session.get(Strategy, strategy_id).status == "running"
        assert session.get(Project, UUID(consumer["id"])).artifact_dependencies
    assert (independent_run / "report.txt").read_bytes() == b"independent strategy run"
    assert client.get(f"/api/v1/artifacts/{published.id}/wheel").content == published_bytes
    assert client.get("/api/v1/artifacts?published=true").json()[0]["id"] == str(published.id)


def test_create_package_identity_reserved_by_published_registry_after_source_delete(projects_client, resources, monkeypatch):
    client, root, engine = projects_client
    identifiers = iter(UUID(value) for value in ("abcd0000-0000-4000-8000-000000000001",
        "abcd0000-0000-4000-8000-000000000002", "ef120000-0000-4000-8000-000000000003"))
    monkeypatch.setattr(views, "uuid4", lambda: next(identifiers))
    project = create_cleanup_project(client)
    artifact(engine, root, project, published=True)
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    replacement = create_cleanup_project(client, name=project["name"])
    assert replacement["packageName"] == "factor-ef12"
    assert json.loads((root / replacement["directory"] / ".solo").read_text())["package_name"] == "factor-ef12"


@pytest.mark.parametrize("kind", ["idle-own", "idle-subtree", "busy-own", "shared-generic"])
def test_shutdown_only_idle_owned_exact_sessions_and_kernels(projects_client, resources, kind):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    other = {"id": "foreign-kernel", "name": "python3", "execution_state": "busy"}
    own = {"id": "own-kernel", "name": f"solo-{project['id']}", "execution_state": "idle"}
    path = "elsewhere/notebook.ipynb"
    if kind in {"idle-subtree", "shared-generic"}:
        own["name"] = "python3"
        path = project["directory"] + "/folder/research.ipynb"
    if kind == "busy-own":
        own["execution_state"] = "busy"
    own_session = {"id": "own-session", "path": path, "kernel": own}
    foreign_session = {"id": "foreign-session", "path": project["directory"] + "-other/notebook.ipynb", "kernel": other}
    resources.kernels = [own, other]
    resources.sessions = [own_session, foreign_session]
    if kind == "shared-generic":
        resources.sessions.append({"id": "shared-foreign", "path": "projects/model/other/notebook.ipynb", "kernel": own})
    status = client.delete(f"/api/v1/projects/{project['id']}").status_code
    deleted = [(method, path) for method, path, _, _ in resources.calls if method == "DELETE" and path.startswith("/api/")]
    if kind.startswith("idle"):
        assert status == 204
        assert deleted == [("DELETE", "/api/sessions/own-session"), ("DELETE", "/api/kernels/own-kernel")]
        assert resources.sessions == [foreign_session] and resources.kernels == [other]
    else:
        assert status == 409 and not deleted
        assert (root / project["directory"]).exists()
        with Session(engine) as session:
            assert session.get(Project, UUID(project["id"])) is not None
            assert session.get(ProjectDeletion, UUID(project["id"])) is None


def test_lease_release_failure_retains_original_token_for_retry(projects_client, resources):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    resources.fail_release = True
    url = f"/api/v1/projects/{project['id']}"
    response = client.delete(url)
    assert response.status_code == 503 and response.json()["detail"]["deleted"] is True
    assert not (root / project["directory"]).exists()
    with Session(engine) as session:
        lease = session.get(ProjectDeletion, UUID(project["id"])).payload["lease"]
        assert lease == resources.lease == resources.calls[0][2]["lease"]
        assert str(UUID(lease)) == lease
        assert session.get(Project, UUID(project["id"])) is None
    resources.fail_release = False
    before = len(resources.calls)
    assert client.delete(url).status_code == 204
    retries = [item for item in resources.calls[before:] if item[1] == "/solo/project/operations"]
    assert [item[0] for item in retries] == ["POST", "DELETE"]
    assert all(item[2]["lease"] == lease for item in retries)
    with Session(engine) as session:
        assert session.get(ProjectDeletion, UUID(project["id"])) is None


@pytest.mark.parametrize("restart", [False, True])
def test_acquire_response_loss_persists_nonce_and_recovers_with_fresh_session(projects_client, resources, monkeypatch, restart):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    url = f"/api/v1/projects/{project['id']}"
    resources.lose_acquire_response = resources.fail_release = True
    original = resources.request
    observed = []

    def request(path, headers, **kwargs):
        if path == "/solo/project/operations" and kwargs.get("method") == "POST":
            with Session(engine) as saved:
                job = saved.get(ProjectDeletion, UUID(project["id"]))
                assert job is not None and job.payload["lease"] == kwargs["body"]["lease"]
                assert job.payload["phase"] == "preparing" and job.delete_files is False
                assert job.project_name == project["name"] and job.kind == project["kind"]
                observed.append(job.payload["lease"])
        return original(path, headers, **kwargs)

    monkeypatch.setattr(deletion, "jupyter_request", request)
    response = client.delete(url + "?delete_files=false")
    assert response.status_code == 503 and "deleted" not in response.json()["detail"]
    with Session(engine) as saved:
        job = saved.get(ProjectDeletion, UUID(project["id"]))
        nonce = job.payload["lease"]
        assert nonce == resources.lease == observed[0] and str(UUID(nonce)) == nonce
        assert saved.get(Project, UUID(project["id"])) is not None
    assert (root / project["directory"]).exists() and kernel_directory(project["id"]).exists()
    assert not list(root.rglob(f".delete-{project['id']}"))
    assert not any(path.startswith("/api/") for _, path, _, _ in resources.calls)
    if restart:
        resources.lease = resources.lease_project = None  # A restarted Jupyter loses only in-memory gates.
    resources.fail_release = False
    assert client.delete(url + "?delete_files=true").status_code == 204
    assert observed == [nonce, nonce]
    assert (root / project["directory"]).exists()  # Original persisted retention intent wins.
    assert resources.lease is None
    with Session(engine) as saved:
        assert saved.get(Project, UUID(project["id"])) is None
        assert saved.get(ProjectDeletion, UUID(project["id"])) is None


@pytest.mark.parametrize("busy", ["source", "wrong-nonce"])
def test_acquire_conflict_never_releases_real_owner_or_changes_source(projects_client, resources, busy):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    workspace = root / project["directory"]
    before = {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    if busy == "source":
        resources.source_busy = True
    else:
        resources.lease, resources.lease_project = str(uuid4()), project["id"]
    owner = resources.lease
    response = client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 409
    assert resources.lease == owner and resources.source_busy == (busy == "source")
    assert [method for method, _, _, _ in resources.calls] == ["POST"]
    assert {path.relative_to(workspace): path.read_bytes() for path in workspace.rglob("*") if path.is_file()} == before
    with Session(engine) as saved:
        assert saved.get(Project, UUID(project["id"])) is not None
        assert saved.get(ProjectDeletion, UUID(project["id"])) is None


@pytest.mark.parametrize("failure", ["before-commit", "after-commit"])
def test_nonce_journal_commit_failure_never_sends_acquire_without_durable_intent(projects_client, resources, monkeypatch, failure):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    with Session(engine) as session:
        original = session.commit
        failed = False

        def commit():
            nonlocal failed
            if not failed:
                failed = True
                if failure == "after-commit":
                    original()
                raise RuntimeError("journal commit acknowledgement failed")
            return original()

        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(RuntimeError, match="journal commit acknowledgement failed"):
            deletion.delete_project(UUID(project["id"]), session)
    assert resources.calls == []
    assert (root / project["directory"]).exists() and kernel_directory(project["id"]).exists()
    with Session(engine) as saved:
        job = saved.get(ProjectDeletion, UUID(project["id"]))
        assert (job is not None) == (failure == "after-commit")
        nonce = job.payload["lease"] if job else None
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    if nonce:
        assert resources.calls[0][2]["lease"] == nonce


def test_deletion_commit_response_loss_keeps_deleted_phase_and_original_uuid_retry(projects_client, resources, monkeypatch):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    identifier = UUID(project["id"])
    with Session(engine) as session:
        original = session.commit
        failed = False

        def commit():
            nonlocal failed
            lost = not failed and any(isinstance(item, Project) and item.id == identifier for item in session.deleted)
            original()
            if lost:
                failed = True
                raise RuntimeError("delete commit acknowledgement lost")

        monkeypatch.setattr(session, "commit", commit)
        with pytest.raises(deletion.HTTPException) as error:
            deletion.delete_project(identifier, session)
        assert error.value.status_code == 503 and error.value.detail["deleted"] is True
    assert not (root / project["directory"]).exists()
    with Session(engine) as saved:
        assert saved.get(Project, identifier) is None
        assert saved.get(ProjectDeletion, identifier).payload["phase"] == "deleted"
    replacement = create_cleanup_project(client, name=project["name"])
    marker = (root / replacement["directory"] / ".solo").read_bytes()
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    assert (root / replacement["directory"] / ".solo").read_bytes() == marker
    assert kernel_directory(replacement["id"]).exists()


def test_lease_release_response_loss_recovers_after_restart_without_unlocking_source(projects_client, resources):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    url = f"/api/v1/projects/{project['id']}"
    resources.lose_release_response = True
    response = client.delete(url)
    assert response.status_code == 503 and response.json()["detail"]["deleted"] is True
    with Session(engine) as saved:
        nonce = saved.get(ProjectDeletion, UUID(project["id"])).payload["lease"]
    assert resources.lease is None
    resources.source_busy = True
    before = len(resources.calls)
    response = client.delete(url)
    assert response.status_code == 503 and response.json()["detail"]["deleted"] is True
    assert [method for method, path, _, _ in resources.calls[before:] if path == "/solo/project/operations"] == ["POST"]
    assert resources.source_busy
    with Session(engine) as saved:
        assert saved.get(ProjectDeletion, UUID(project["id"])).payload["lease"] == nonce
    resources.source_busy = False
    assert client.delete(url).status_code == 204
    assert resources.lease is None


def test_legacy_opaque_journal_lease_is_reused_without_rekeying(projects_client, resources):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    identifier = UUID(project["id"])
    legacy = "legacy-jupyter-issued-delete-token"
    with Session(engine) as saved:
        saved.add(ProjectDeletion(id=identifier, project_name=project["name"], kind=project["kind"], delete_files=True,
            payload={"format": 1, "phase": "preparing", "run_ids": [],
                     "paths": deletion.resource_plan(identifier, project["name"], project["kind"], True, []),
                     "instances": [], "lease": legacy}, error=""))
        saved.commit()
    resources.lease, resources.lease_project = legacy, project["id"]
    assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 204
    leases = [body["lease"] for _, path, body, _ in resources.calls if path == "/solo/project/operations"]
    assert leases == [legacy, legacy] and resources.lease is None


@pytest.mark.parametrize("invalid", ["paths", "identity", "lease"])
def test_lost_acquire_retry_cannot_bypass_original_uuid_journal_validation(projects_client, resources, invalid):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    url = f"/api/v1/projects/{project['id']}"
    resources.lose_acquire_response = resources.fail_release = True
    assert client.delete(url).status_code == 503
    with Session(engine) as saved:
        job = saved.get(ProjectDeletion, UUID(project["id"]))
        if invalid == "identity":
            job.project_name = "another-project"
        elif invalid == "paths":
            job.payload = {**job.payload, "paths": []}
        else:
            job.payload = {**job.payload, "lease": {"invalid": "nonce"}}
        saved.commit()
    resources.fail_release = False
    before = len(resources.calls)
    assert client.delete(url).status_code == 409
    assert len(resources.calls) == before and resources.lease is not None
    assert (root / project["directory"]).exists()
    with Session(engine) as saved:
        assert saved.get(ProjectDeletion, UUID(project["id"])) is not None


def test_staging_rollback_covers_runs_and_kernel_in_own_mount(projects_client, resources, monkeypatch):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    run_id, run = add_version(engine, root, project)
    workspace = root / project["directory"]
    kernel = kernel_directory(project["id"])
    original = Path.rename
    observed = []

    def rename(source, target):
        target = Path(target)
        observed.append((source, target))
        if source == kernel:
            assert target.parent.parent == source.parent  # No cross-device rename into /shared.
            raise OSError("kernelspec mount unavailable")
        return original(source, target)

    monkeypatch.setattr(Path, "rename", rename)
    response = client.delete(f"/api/v1/projects/{project['id']}")
    assert response.status_code == 503
    assert (workspace / ".solo").is_file() and (run / "input.json").is_file() and (kernel / "kernel.json").is_file()
    assert any(source == run for source, _ in observed)
    assert not list(root.rglob(f".delete-{project['id']}"))
    with Session(engine) as session:
        assert session.get(Project, UUID(project["id"])) is not None
        assert session.get(ProjectVersion, run_id) is not None
        assert session.get(ProjectDeletion, UUID(project["id"])) is None


def scheduler_api(monkeypatch, instances, *, deletion_result=None, keep_after_delete=False):
    client = DolphinSchedulerClient()
    client.project_code = Mock(return_value=10)
    client.definition = Mock(return_value={"code": 20})
    calls = []

    def request(method, path, params=None):
        calls.append((method, path, params))
        if method == "GET" and path.endswith("/process-instances"):
            assert params["pageNo"] == 1 and params["processDefineCode"] == 0
            return {"total": len(instances), "totalList": list(instances)}
        if method == "GET":
            return next(item for item in instances if path.endswith(f"/{item['id']}"))
        assert method == "DELETE" and path.startswith("/projects/10/process-instances/")
        if deletion_result is None and not keep_after_delete:
            instances[:] = [item for item in instances if not path.endswith(f"/{item['id']}")]
        return deletion_result

    client.request = Mock(side_effect=request)
    factory = MagicMock()
    factory.return_value.__enter__.return_value = client
    factory.return_value.__exit__.side_effect = lambda *args: client.session.close()
    monkeypatch.setattr(deletion, "DolphinSchedulerClient", factory)
    monkeypatch.setattr(DolphinSchedulerSettings, "ENABLED", True)
    return client, calls


@pytest.mark.parametrize("failure", [None, "false", "not-deleted", "wrong-owner", "wrong-definition", "busy", "incomplete"])
def test_scheduler_cleanup_is_verified_and_never_deletes_shared_definitions(projects_client, resources, monkeypatch, failure):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    run_id, run = add_version(engine, root, project, workflow_id=42)
    input_file = str(run / "input.json")
    owned = {"id": 42, "processDefinitionCode": 20, "projectCode": 10, "state": "SUCCESS",
             "globalParams": json.dumps([{"prop": "input_file", "value": input_file}])}
    unrelated = {"id": 43, "processDefinitionCode": 20, "state": "RUNNING_EXECUTION", "globalParams": "[]"}
    if failure == "wrong-owner":
        owned["globalParams"] = json.dumps([{"prop": "input_file", "value": "/shared/runs/another/input.json"}])
    elif failure == "wrong-definition":
        owned["processDefinitionCode"] = 99
    elif failure == "busy":
        owned["state"] = "RUNNING_EXECUTION"
    instances = [owned, unrelated]
    scheduler, calls = scheduler_api(monkeypatch, instances, deletion_result=False if failure == "false" else None,
                                    keep_after_delete=failure == "not-deleted")
    if failure == "incomplete":
        scheduler.instance_index = Mock(side_effect=DolphinSchedulerError("API incomplete"))
    url = f"/api/v1/projects/{project['id']}"
    response = client.delete(url)
    if failure is None:
        assert response.status_code == 204 and instances == [unrelated]
        assert not run.exists()
        assert [(method, path) for method, path, _ in calls if method == "DELETE"] == [("DELETE", "/projects/10/process-instances/42")]
    elif failure in {"false", "not-deleted"}:
        assert response.status_code == 503 and response.json()["detail"]["deleted"] is True
        assert root.joinpath("runs", f".delete-{project['id']}", str(run_id)).exists()
        assert instances == [owned, unrelated]
        scheduler, retry_calls = scheduler_api(monkeypatch, instances)
        assert client.delete(url).status_code == 204 and instances == [unrelated]
        assert any(method == "DELETE" for method, _, _ in retry_calls)
    else:
        assert response.status_code == (409 if failure == "busy" else 503)
        assert (root / project["directory"]).exists() and run.exists()
        assert not any(method == "DELETE" for method, _, _ in calls)
    assert not any("log" in path or "process-definition" in path for _, path, _ in calls)


@pytest.mark.parametrize("mode", [True, False])
@pytest.mark.parametrize("phase", ["building", "queued", "running"])
def test_active_phase_guard_runs_before_any_resource_api(projects_client, resources, mode, phase):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    add_version(engine, root, project, status=phase)
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files={str(mode).lower()}").status_code == 409
    assert resources.calls == []


@pytest.mark.parametrize("mode", [True, False])
def test_orphan_original_uuid_staging_cannot_be_adopted(projects_client, resources, mode):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    kernel = kernel_directory(project["id"])
    stage = kernel.parent / f".delete-{project['id']}"
    stage.mkdir()
    (stage / "unrelated.json").write_bytes(b"unowned")
    assert client.delete(f"/api/v1/projects/{project['id']}?delete_files={str(mode).lower()}").status_code == 409
    assert resources.calls == [] and (stage / "unrelated.json").read_bytes() == b"unowned"


@pytest.mark.parametrize("global_params", ['not-json', '{}', '["bad"]', '[{"prop":"input_file","value":1}]',
    '[{"prop":"input_file","value":"a"},{"prop":"input_file","value":"b"}]'])
def test_scheduler_input_identity_fails_closed(global_params):
    with pytest.raises(DolphinSchedulerError):
        instance_input_file({"globalParams": global_params})


def test_deletion_guard_prevents_same_uuid_reentrant_cleanup(projects_client, resources):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    with Session(engine) as session:
        with deletion.project_lifecycle_guard(UUID(project["id"]), session):
            assert client.delete(f"/api/v1/projects/{project['id']}").status_code == 409
    assert resources.calls == [] and (root / project["directory"]).exists()


@pytest.mark.parametrize("outcome", ["success", "prepare-failure", "commit-failure"])
def test_delete_excluded_through_rename_prepare_commit_rollback_and_cleanup(
        projects_client, resources, monkeypatch, outcome):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    unrelated = create_cleanup_project(client, name="unrelated")
    identifier = UUID(project["id"])
    source = root / project["directory"]
    destination = source.with_name("renamed")
    (source / ".venv" / "installed.txt").write_bytes(b"original environment")
    before = {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    kernel = kernel_directory(project["id"]) / "kernel.json"
    old_kernel = kernel.read_bytes()
    run_id, run = add_version(engine, root, project)
    with Session(engine) as session:
        # SQLite reloads timestamps as naive; use two persisted endpoints for read_version.
        session.get(ProjectVersion, run_id).finished_at = datetime.now(UTC)
        session.commit()
    phases = ["prepare", *([] if outcome == "prepare-failure" else ["commit"]),
              *([] if outcome == "success" else ["rollback"]), "cleanup"]
    barriers = {phase: (Event(), Event()) for phase in phases}
    original_prepare = views.prepare_environment
    original_rename = Path.rename
    original_rmtree = views.shutil.rmtree

    def pause(phase):
        entered, resume = barriers[phase]
        entered.set()
        assert resume.wait(10), f"test did not release rename {phase}"

    def prepare(directory, project_id, name, *, locked=False):
        assert directory == destination and locked is True
        pause("prepare")  # DELETE must conflict even before prepare rewrites the old kernelspec.
        original_prepare(directory, project_id, name, locked=locked)
        (directory / ".venv" / "partial.txt").write_bytes(b"rebuilt environment")
        (directory / "research.ipynb").write_bytes(b'{"cells":[],"metadata":{"changed":true}}')
        if outcome == "prepare-failure":
            raise EnvironmentError("injected prepare failure")

    def rename(path, target):
        result = original_rename(path, target)
        if path == destination and Path(target) == source:
            pause("rollback")  # DB rolled back; metadata/kernel restoration is still in progress.
        return result

    def cleanup(path, *args, **kwargs):
        path = Path(path)
        if path.parent == source.parent and path.name.startswith(".rename-"):
            pause("cleanup")  # Even a committed rename must exclude DELETE until backup cleanup.
        return original_rmtree(path, *args, **kwargs)

    def edit():
        with Session(engine, expire_on_commit=False) as session:
            original_commit = session.commit

            def commit():
                if outcome == "commit-failure":
                    pause("commit")
                    raise RuntimeError("injected rename commit failure")
                original_commit()
                pause("commit")  # Session-level exclusion must survive a successful commit.

            session.commit = commit
            try:
                return views.edit_project(identifier, ProjectEdit(name="renamed", description="new"), session)
            except views.HTTPException as error:
                return error

    monkeypatch.setattr(views, "prepare_environment", prepare)
    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(views.shutil, "rmtree", cleanup)
    with Session(engine, expire_on_commit=False) as deleting:
        cached = deleting.get(Project, identifier)
        deleting.commit()  # Preserve stale ORM identity, without retaining an old read transaction.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(edit)
            try:
                for phase in phases:
                    entered, resume = barriers[phase]
                    assert entered.wait(10), f"rename did not reach {phase}"
                    if phase == "prepare":
                        assert not source.exists() and destination.is_dir()
                        assert kernel.read_bytes() == old_kernel
                    with pytest.raises(views.HTTPException) as conflict:
                        views.remove_project(identifier, deleting, delete_files=True)
                    assert conflict.value.status_code == 409
                    assert resources.calls == []
                    with Session(engine) as observer:
                        assert observer.get(ProjectDeletion, identifier) is None
                    resume.set()
                result = future.result(timeout=10)
            finally:
                for _, resume in barriers.values():
                    resume.set()

        if outcome == "success":
            assert result.name == "renamed"
            current = destination
            assert cached.name == project["name"]  # DELETE must refresh this cached old workspace name.
        else:
            assert isinstance(result, views.HTTPException)
            assert result.status_code == (502 if outcome == "prepare-failure" else 503)
            current = source
            assert {path.relative_to(source): path.read_bytes() for path in source.rglob("*") if path.is_file()} == before
            assert kernel.read_bytes() == old_kernel
        assert not list(source.parent.glob(".rename-*"))
        assert json.loads((current / ".solo").read_text())["name"] == current.name
        assert json.loads(kernel.read_text())["argv"][0] == str(current / ".venv" / "bin" / "python")

        own = {"id": "current-kernel", "name": "python3", "execution_state": "idle"}
        uuid_kernel = {"id": "uuid-kernel", "name": f"solo-{identifier}", "execution_state": "idle"}
        foreign = {"id": "foreign-kernel", "name": "python3", "execution_state": "busy"}
        foreign_session = {"id": "foreign-session", "path": unrelated["directory"] + "/research.ipynb", "kernel": foreign}
        resources.kernels = [own, uuid_kernel, foreign]
        resources.sessions = [
            {"id": "current-session", "path": current.relative_to(root).as_posix() + "/research.ipynb", "kernel": own},
            {"id": "uuid-session", "path": "elsewhere/research.ipynb", "kernel": uuid_kernel},
            foreign_session,
        ]
        assert views.remove_project(identifier, deleting, delete_files=True).status_code == 204
    assert not source.exists() and not destination.exists() and not kernel.parent.exists()
    assert not run.exists() and not list(root.rglob(f".delete-{identifier}"))
    assert resources.sessions == [foreign_session] and resources.kernels == [foreign]
    assert resources.lease is None
    assert (root / unrelated["directory"] / "research.ipynb").is_file()
    assert kernel_directory(unrelated["id"]).is_dir()
    with Session(engine) as observer:
        assert observer.get(Project, identifier) is None
        assert observer.get(ProjectVersion, run_id) is None
        assert observer.get(ProjectDeletion, identifier) is None


@pytest.mark.parametrize("name", ["cleanup", "renamed"])
def test_patch_acquires_lifecycle_exclusion_before_journal_or_project_reads(
        projects_client, resources, monkeypatch, name):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    entered, resume = Event(), Event()
    original = views.require_not_deleting

    def check(session, identifier):
        entered.set()
        assert resume.wait(10), "test did not release PATCH journal check"
        original(session, identifier)

    monkeypatch.setattr(views, "require_not_deleting", check)
    url = f"/api/v1/projects/{project['id']}"
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.patch, url, json={"name": name, "description": "new"})
        try:
            assert entered.wait(10)
            response = client.delete(url)
            assert response.status_code == 409, response.text
            assert resources.calls == []
            assert (root / project["directory"]).is_dir()
            with Session(engine) as observer:
                assert observer.get(ProjectDeletion, UUID(project["id"])) is None
        finally:
            resume.set()
        result = future.result(timeout=10)
    assert result.status_code == 200, result.text
    assert result.json()["name"] == name and result.json()["description"] == "new"
    assert client.delete(url).status_code == 204
    assert not (root / "projects" / project["kind"] / name).exists()
    assert not kernel_directory(project["id"]).exists()


@pytest.mark.parametrize("name", ["cleanup", "renamed"])
def test_busy_lifecycle_rejects_patch_before_checks(projects_client, resources, monkeypatch, name):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    check = Mock(side_effect=AssertionError("busy PATCH must not read the journal/project"))
    monkeypatch.setattr(views, "require_not_deleting", check)
    monkeypatch.setattr(views, "get_project_or_404", check)
    with Session(engine) as deleting:
        with deletion.project_lifecycle_guard(UUID(project["id"]), deleting):
            response = client.patch(f"/api/v1/projects/{project['id']}", json={"name": name})
            assert response.status_code == 409, response.text
    check.assert_not_called()
    assert resources.calls == [] and (root / project["directory"]).is_dir()


@pytest.mark.parametrize("name", ["cleanup", "renamed"])
def test_pending_deletion_journal_still_rejects_patch(projects_client, resources, name):
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    with Session(engine) as session:
        session.add(ProjectDeletion(id=UUID(project["id"]), project_name=project["name"], kind=project["kind"],
                                    delete_files=True, payload={}, error="recovery pending"))
        session.commit()
    response = client.patch(f"/api/v1/projects/{project['id']}", json={"name": name})
    assert response.status_code == 409 and "清理" in response.json()["detail"]
    assert resources.calls == [] and (root / project["directory"]).is_dir()


def test_postgresql_lifecycle_guard_excludes_other_workers_across_commits_and_rollbacks(monkeypatch):
    identifier = uuid4()
    key = identifier.int & ((1 << 63) - 1)
    owner, contender = Mock(), Mock()
    for session in (owner, contender):
        session.get_bind.return_value.dialect.name = "postgresql"
    owned = owner.get_bind.return_value.connect.return_value
    competing = contender.get_bind.return_value.connect.return_value
    owned.scalar.return_value = True
    competing.scalar.return_value = False
    with deletion.project_lifecycle_guard(identifier, owner):
        owner.commit()
        owner.rollback()
        with monkeypatch.context() as worker:
            worker.setattr(deletion, "_lifecycle_projects", set())  # Independent worker's process set.
            with pytest.raises(views.HTTPException) as conflict:
                with deletion.project_lifecycle_guard(identifier, contender):
                    pytest.fail("another worker acquired the held project lifecycle lock")
            assert conflict.value.status_code == 409
            assert not deletion._lifecycle_projects
        assert owned.execute.call_count == 0
        assert owned.close.call_count == 0
        assert competing.execute.call_count == 0  # Rejected acquisition must not unlock the owner.
        competing.close.assert_called_once()
    assert str(owned.scalar.call_args.args[0]) == "SELECT pg_try_advisory_lock(:key)"
    assert owned.scalar.call_args.args[1] == {"key": key}
    assert competing.scalar.call_args.args[1] == {"key": key}
    assert str(owned.execute.call_args.args[0]) == "SELECT pg_advisory_unlock(:key)"
    assert owned.execute.call_args.args[1] == {"key": key}
    owned.close.assert_called_once()
    assert identifier not in deletion._lifecycle_projects


@pytest.mark.parametrize("failure", ["acquire", "unlock", "close"])
def test_lifecycle_guard_releases_process_exclusion_on_connection_failure(failure):
    identifier = uuid4()
    session = Mock()
    session.get_bind.return_value.dialect.name = "postgresql"
    connection = session.get_bind.return_value.connect.return_value
    connection.scalar.return_value = True
    operation = {"acquire": connection.scalar, "unlock": connection.execute, "close": connection.close}[failure]
    operation.side_effect = RuntimeError(failure)
    with pytest.raises(RuntimeError, match=failure):
        with deletion.project_lifecycle_guard(identifier, session):
            pass
    assert identifier not in deletion._lifecycle_projects
    if failure in {"acquire", "unlock"}:
        connection.invalidate.assert_called_once()
    connection.close.assert_called_once()
    local = Mock()
    local.get_bind.return_value.dialect.name = "sqlite"
    with deletion.project_lifecycle_guard(identifier, local):
        pass


def test_read_metadata_preserves_large_custom_fields(tmp_path):
    path = tmp_path / ".solo"
    metadata = {"project_id": str(uuid4()), "custom": "x" * 70000}
    path.write_text(json.dumps(metadata), encoding="utf-8")
    assert deletion.read_metadata(path) == metadata


@pytest.mark.parametrize("target", ["boundary", "outside"])
def test_checked_directory_rejects_resolved_boundary_and_escape(tmp_path, target):
    boundary = tmp_path / "shared"
    boundary.mkdir()
    directory = boundary if target == "boundary" else tmp_path / "outside"
    with pytest.raises(views.HTTPException) as error:
        deletion.checked_directory(directory, boundary)
    assert error.value.status_code == 409


@pytest.mark.parametrize("target", ["inside", "boundary", "outside"])
def test_checked_directory_uses_resolved_link_target_without_creation_privileges(tmp_path, monkeypatch, target):
    boundary = tmp_path / "shared"
    alias = boundary / "alias"
    alias.mkdir(parents=True)
    resolved = {"inside": boundary / "real", "boundary": boundary, "outside": tmp_path / "outside"}[target]
    original = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda path, **kwargs: resolved if path == alias else original(path, **kwargs))
    if target == "inside":
        assert deletion.checked_directory(alias, boundary)
    else:
        with pytest.raises(views.HTTPException) as error:
            deletion.checked_directory(alias, boundary)
        assert error.value.status_code == 409


def test_checked_directory_accepts_in_boundary_link_and_rejects_link_to_boundary(tmp_path):
    child = tmp_path / "real"
    child.mkdir()
    link, root_link = tmp_path / "alias", tmp_path / "root-alias"
    try:
        link.symlink_to(child, target_is_directory=True)
        root_link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("系统不允许创建符号链接")
    assert deletion.checked_directory(link, tmp_path)
    with pytest.raises(views.HTTPException):
        deletion.checked_directory(root_link, tmp_path)


@pytest.mark.parametrize("location", ["project", "kind"])
def test_delete_accepts_directory_links_within_boundary_without_deleting_link_target(
        projects_client, resources, location):
    client, root, _ = projects_client
    project = create_cleanup_project(client)
    directory = root / project["directory"]
    source = directory if location == "project" else directory.parent
    target = root / f"aliased-{location}"
    source.rename(target)
    try:
        source.symlink_to(target, target_is_directory=True)
    except OSError:
        target.rename(source)
        pytest.skip("系统不允许创建符号链接")
    response = client.delete(f'/api/v1/projects/{project["id"]}')
    assert response.status_code == 204, response.text
    if location == "project":
        assert not source.is_symlink() and (target / "research.ipynb").is_file()
    else:
        assert source.is_symlink() and target.is_dir()
        assert not (target / project["name"]).exists()
