"""Real-policy admission boundaries; all state is in SQLite memory and temp runs."""

import hashlib
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.projects import templates, versions, views
from core.apps.projects.models import Project, ProjectVersion
from core.apps.projects.schemas import ProjectCreate, TemplateVersion
from core.apps.strategies import service
from core.apps.strategies import views as strategy_views
from core.apps.strategies.models import Strategy
from core.database.base import Base
from core.database.session import get_database_session
from fastapi import HTTPException
from fastapi.testclient import TestClient
from main import app
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

SCHEME_OLD = "44e636ac754e0316d7142674ebad022a14b51b78"
TEMPLATE_OLD = "17546a61e3bd602f4f3c7598e996d531e804dbcd"


@pytest.fixture
def policy_client(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(SoloSettings, "JUPYTER_TOKEN", "")
    session = Session(engine, expire_on_commit=False)
    app.dependency_overrides[get_database_session] = lambda: session
    with TestClient(app) as client:
        yield client, session, tmp_path
    app.dependency_overrides.clear()
    session.close()
    engine.dispose()


def project_record(session, root, *, scheme="1.2.0", scheme_tag="v1.2.0", scheme_commit="b" * 40,
                   template_tag="v1.2.0", template_commit="a" * 40, kind="model", archived=False):
    project = Project(id=uuid4(), name=str(uuid4()), description="", kind=kind,
                      scheme_version=scheme_tag, scheme_commit=scheme_commit,
                      template_tag=template_tag, template_commit=template_commit, archived=archived)
    session.add(project)
    session.commit()
    directory = root / "projects" / kind / project.name
    directory.mkdir(parents=True)
    (directory / "uv.lock").write_text(f'[[package]]\nname="scheme"\nversion="{scheme}"\n')
    (directory / "research.ipynb").write_text('{"cells":[]}')
    return project


def task_record(session, root, project, *, scheme="1.2.0", scheme_commit="b" * 40,
                status="building", workflow_id=None, recorded=None):
    number = len(session.scalars(select(ProjectVersion).where(ProjectVersion.project_id == project.id)).all()) + 1
    series = ".".join(scheme.split(".")[:2])
    record = ProjectVersion(id=uuid4(), project_id=project.id, number=number,
                            package_version=f"{series}.{number}", status=status, workflow_id=workflow_id,
                            scheme_version=recorded, parameters={}, note="", error="")
    session.add(record)
    session.commit()
    directory = root / "runs" / str(record.id)
    directory.mkdir(parents=True)
    lockfile = directory / "environment" / "uv.lock"
    lockfile.parent.mkdir()
    lockfile.write_text(f'[[package]]\nname="scheme"\nversion="{scheme}"\n'
                        f'source={{git="https://example.invalid/scheme#{scheme_commit}"}}\n')
    wheel = directory / "candidate.whl"
    package = f"{project.kind}-{project.id.hex[:4]}"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package.replace('-', '_')}-{record.package_version}.dist-info/METADATA",
                         f"Metadata-Version: 2.1\nName: {package}\nVersion: {record.package_version}\n"
                         f"Requires-Dist: scheme>={series}.0,<{series.split('.')[0]}.{int(series.split('.')[1]) + 1}.0\n")
        archive.writestr(package.replace('-', '_') + "/__init__.py", "")
    with lockfile.open("a") as stream:
        stream.write(f'[[package]]\nname="{package}"\nversion="{record.package_version}"\nsource={{path="../candidate.whl"}}\n')
    component = {"package": package, "version": record.package_version,
                 "entry": f"{package.replace('-', '_')}:Algo",
                 "wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "params": {}}
    data = {"kind": project.kind, "environment": {"lockfile": str(lockfile)}, "output": str(directory / "report")}
    if project.kind == "factor":
        data.update(factor=component, analysis={})
    else:
        data.update(algos={project.kind: component}, backtest={})
    (directory / "input.json").write_text(json.dumps(data))
    (directory / "build.json").write_text(json.dumps({"scheme_version": scheme}))
    record.input_sha256 = hashlib.sha256((directory / "input.json").read_bytes()).hexdigest()
    record.lock_sha256 = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    session.commit()
    return record, directory


def hashes(directory, kind):
    data = json.loads((directory / "input.json").read_text())
    return {"kind": kind, "input_sha256": hashlib.sha256((directory / "input.json").read_bytes()).hexdigest(),
            "lock_sha256": hashlib.sha256(Path(data["environment"]["lockfile"]).read_bytes()).hexdigest()}


def test_public_policy_is_single_json_without_db_or_network(monkeypatch):
    session = MagicMock(side_effect=AssertionError("must not access DB"))
    app.dependency_overrides[get_database_session] = session
    try:
        with TestClient(app) as client:
            response = client.get("/api/v1/version-policy")
        assert response.status_code == 200
        assert response.json() == json.loads(policy.POLICY_FILE.read_text(encoding="utf-8"))
        body = response.json()
        assert body["minimum_versions"] == {"scheme": "1.2.0", "template": "1.2.0"}
        assert body["grandfathered_runs"] == []
        assert datetime.fromisoformat(body["retired_at"]) <= datetime.now(UTC)
        assert len(body["retired_releases"]["scheme"]) == 4
        assert len(body["retired_releases"]["template"]) == 3
        assert all(release["reason"] for items in body["retired_releases"].values() for release in items)
        session.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_no_nonempty_grandfather_configuration(tmp_path, monkeypatch):
    body = policy.load_policy()
    body["grandfathered_runs"] = [{"run_id": str(uuid4()), "workflow_id": 1, "kind": "factor",
                                  "input_sha256": "a" * 64, "lock_sha256": "b" * 64}]
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(body))
    monkeypatch.setattr(policy, "POLICY_FILE", path)
    with pytest.raises(policy.VersionPolicyError, match="不支持启用 grandfather"):
        policy.load_policy()


@pytest.mark.parametrize("source", ["scheme_tag", "scheme_commit", "template_tag", "template_commit", "actual"])
def test_retired_history_visible_but_project_and_save_admission_blocked(policy_client, monkeypatch, source):
    client, session, root = policy_client
    kwargs = {
        "scheme_tag": {"scheme_tag": "v1.1.0"},
        "scheme_commit": {"scheme_tag": "not-semver", "scheme_commit": SCHEME_OLD},
        "template_tag": {"template_tag": "v1.0.1"},
        "template_commit": {"template_tag": "not-semver", "template_commit": TEMPLATE_OLD},
        "actual": {"scheme": "1.1.7", "scheme_tag": "not-semver"},
    }[source]
    project = project_record(session, root, **kwargs)
    before = project.updated_at
    url = f"/api/v1/projects/{project.id}"
    history = client.get(url)
    assert history.status_code == 200
    assert history.json()["retired"] is True
    assert history.json()["retiredReason"]
    listing = client.get("/api/v1/projects").json()
    assert len(listing) == 1 and listing[0]["id"] == str(project.id)
    assert client.get(url + "/jupyter", follow_redirects=False).status_code == 307
    for endpoint in (f"/api/v1/version-policy/projects/{project.id}/check", url + "/versions"):
        response = client.post(endpoint, json={})
        assert response.status_code == 422, response.text
        assert response.json()["detail"]["code"] == "version_retired"
    assert session.scalar(select(ProjectVersion)) is None
    assert project.updated_at == before
    # No HTTP-only guard: the same direct Python save call is blocked.
    with pytest.raises(HTTPException) as error:
        versions.create_version(project.id, versions.CreateVersion(), session)
    assert error.value.status_code == 422


def test_project_check_is_readonly_and_missing_or_archived_is_explicit(policy_client, monkeypatch):
    client, session, root = policy_client
    project = project_record(session, root, scheme_tag="non-semver-active")
    commit = MagicMock(side_effect=AssertionError("read-only"))
    monkeypatch.setattr(session, "commit", commit)
    assert client.post(f"/api/v1/version-policy/projects/{project.id}/check").json() == {"allowed": True}
    assert client.post(f"/api/v1/version-policy/projects/{uuid4()}/check").status_code == 404
    project.archived = True
    assert client.post(f"/api/v1/version-policy/projects/{project.id}/check").json()["detail"]["code"] == "project_archived"
    commit.assert_not_called()


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution"])
def test_task_check_readonly_active_and_identity_hash_mismatch(policy_client, monkeypatch, kind):
    client, session, root = policy_client
    project = project_record(session, root, kind=kind)
    record, directory = task_record(session, root, project, status="queued")
    body = hashes(directory, kind)
    commit = MagicMock(side_effect=AssertionError("read-only"))
    monkeypatch.setattr(session, "commit", commit)
    endpoint = f"/api/v1/version-policy/tasks/{record.id}/check"
    assert client.post(endpoint, json=body).json() == {"allowed": True, "grandfathered": False}
    for field, value in (("input_sha256", "0" * 64), ("lock_sha256", "0" * 64), ("kind", "strategy")):
        response = client.post(endpoint, json={**body, field: value})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "task_identity_mismatch"
    assert client.post(endpoint, json={**body, "path": str(directory)}).status_code == 422
    assert client.post(f"/api/v1/version-policy/tasks/{uuid4()}/check", json=body).json()["detail"]["code"] == "task_not_found"
    commit.assert_not_called()


@pytest.mark.parametrize("state,workflow", [("building", None), ("submit_failed", None), ("queued", None),
                                             ("queued", 42), ("running", 42), ("success", 42)])
def test_retired_task_never_grandfathered_by_old_creation_or_workflow(policy_client, state, workflow):
    client, session, root = policy_client
    project = project_record(session, root)
    record, directory = task_record(session, root, project, scheme="1.1.0", scheme_commit=SCHEME_OLD,
                                    status=state, workflow_id=workflow, recorded="1.1.0")
    record.created_at = datetime.fromisoformat(policy.load_policy()["retired_at"]) - timedelta(days=1)
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "version_retired"
    assert versions.read_version(record)["retired"] is True


@pytest.mark.parametrize("status", ["building", "submit_failed"])
@pytest.mark.parametrize("source", ["actual", "commit", "template"])
def test_direct_submit_retry_retired_is_blocked_before_scheduler(policy_client, monkeypatch, status, source):
    _, session, root = policy_client
    project = project_record(session, root, **({"template_commit": TEMPLATE_OLD} if source == "template" else {}))
    record, directory = task_record(session, root, project,
        scheme="1.1.7" if source == "actual" else "1.2.0",
        scheme_commit=SCHEME_OLD if source == "commit" else "b" * 40, status=status)
    client = MagicMock(side_effect=AssertionError("must not submit"))
    monkeypatch.setattr(versions, "DolphinSchedulerClient", client)
    with pytest.raises(HTTPException) as error:
        versions.submit_version(project.id, record.id, versions.BuildResult(), session)
    assert error.value.status_code == 422 and error.value.detail["code"] == "version_retired"
    assert record.status == status
    client.assert_not_called()


def test_old_result_stays_retired_when_current_project_is_active(policy_client):
    client, session, root = policy_client
    project = project_record(session, root)
    record, _ = task_record(session, root, project, scheme="1.0.1", status="success", recorded="1.0.1")
    data = client.get(f"/api/v1/projects/{project.id}").json()
    assert data["retired"] is False
    assert data["versions"][0]["retired"] is True
    assert data["versions"][0]["reportPath"] == f"{record.id}/report"
    assert record.scheme_version == "1.0.1"


def test_known_upstream_template_source_cannot_bypass_retirement(policy_client):
    client, session, root = policy_client
    project = project_record(session, root)
    upstream = project_record(session, root, kind="factor", template_commit=TEMPLATE_OLD)
    record, directory = task_record(session, root, project, status="queued")
    lock = directory / "environment/uv.lock"
    with lock.open("a") as stream:
        stream.write(f'[[package]]\nname="factor-{upstream.id.hex[:4]}"\nversion="1.2.99"\n')
    record.lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest()
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 422 and response.json()["detail"]["code"] == "version_retired"


@pytest.mark.parametrize("escape", ["lock", "input", "run"])
def test_task_checks_cannot_read_outside_mapped_run(policy_client, escape):
    client, session, root = policy_client
    project = project_record(session, root)
    record, directory = task_record(session, root, project)
    body = hashes(directory, "model")
    data = json.loads((directory / "input.json").read_text())
    if escape == "lock":
        path = root / "outside/uv.lock"
        path.parent.mkdir()
        path.write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
        data["environment"]["lockfile"] = str(path)
        (directory / "input.json").write_text(json.dumps(data))
        body = {**body, "input_sha256": hashlib.sha256((directory / "input.json").read_bytes()).hexdigest()}
    else:
        target = root / "outside"
        target.mkdir()
        if escape == "input":
            path = target / "input.json"
            path.write_text(json.dumps(data))
            (directory / "input.json").unlink()
            link, destination = directory / "input.json", path
        else:
            import shutil

            shutil.copytree(directory, target / "run")
            shutil.rmtree(directory)
            link, destination = directory, target / "run"
        try:
            link.symlink_to(destination, target_is_directory=escape == "run")
        except OSError:
            pytest.skip("symlinks unavailable")
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=body)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "task_identity_mismatch"


def test_task_actual_scheme_must_match_frozen_record(policy_client):
    client, session, root = policy_client
    project = project_record(session, root)
    record, directory = task_record(session, root, project, recorded="1.2.7", status="queued")
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 422
    assert "ActualScheme" in response.json()["detail"]["reason"]


@pytest.mark.parametrize("kind", ["scheme", "template"])
def test_catalog_skips_retired_commit_without_metadata_network(monkeypatch, kind):
    old_commit = SCHEME_OLD if kind == "scheme" else TEMPLATE_OLD
    active = TemplateVersion(tag="v1.2.0", commit="c" * 40, version="1.2.0")
    retired = TemplateVersion(tag="invalid-tag", commit=old_commit)
    monkeypatch.setattr(templates, "repository_versions", lambda *args: [retired, active])
    calls = []

    def metadata(repository, commit, path):
        calls.append(commit)
        assert commit != old_commit
        return {"version": "1.2.0", "dependencies": ["scheme>=1.2.0,<1.3.0"]}

    monkeypatch.setattr(templates, "project_metadata", metadata)
    if kind == "scheme":
        assert templates.list_scheme_versions() == [active]
    else:
        monkeypatch.setattr(templates, "select_scheme", lambda _: active)
        assert templates.list_versions("factor", active.tag) == [active]
    assert old_commit not in calls


@pytest.mark.parametrize("source", ["scheme", "template"])
def test_create_project_direct_bypass_filtered_catalog_still_blocked(tmp_path, monkeypatch, source):
    scheme = TemplateVersion(tag="active-custom", commit=SCHEME_OLD if source == "scheme" else "b" * 40, version="1.2.0")
    algo = TemplateVersion(tag="active-custom", commit=TEMPLATE_OLD if source == "template" else "a" * 40, version="1.2.0")
    monkeypatch.setattr(templates, "select_scheme", lambda _: scheme)
    monkeypatch.setattr(templates, "list_versions", lambda *args: [algo])
    create = MagicMock(side_effect=AssertionError("must not write project"))
    monkeypatch.setattr(templates, "create_directory", create)
    session = MagicMock()
    with pytest.raises(HTTPException) as error:
        views.create_project(ProjectCreate(name="blocked", kind="factor", scheme_version="active-custom", algo_version="active-custom"), session)
    assert error.value.status_code == 422 and error.value.detail["code"] == "version_retired"
    create.assert_not_called()
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_pin_scheme_direct_retired_commit_never_writes(tmp_path):
    path = tmp_path / "pyproject.toml"
    original = '[project]\nname="factor-abcd"\nversion="1.2.0"\ndependencies=["scheme>=1.2.0,<1.3.0"]\n'
    path.write_text(original)
    with pytest.raises(policy.VersionPolicyError):
        templates.pin_scheme(tmp_path, TemplateVersion(tag="invalid-tag", commit=SCHEME_OLD, version="1.2.0"))
    assert path.read_text() == original


@pytest.mark.parametrize("endpoint", ["forms", "create", "selected"])
@pytest.mark.parametrize("source", ["actual", "template"])
def test_strategy_selection_rejects_old_result_or_root_template(policy_client, monkeypatch, endpoint, source):
    _, session, root = policy_client
    project = project_record(session, root, **({"template_commit": TEMPLATE_OLD} if source == "template" else {}))
    version, _ = task_record(session, root, project, scheme="1.1.7" if source == "actual" else "1.2.0",
                            status="success", recorded="1.1.7" if source == "actual" else "1.2.0")
    inspect = MagicMock(side_effect=AssertionError("must not execute research"))
    monkeypatch.setattr(service, "command", inspect)
    selection = strategy_views.Selection(model=version.id)
    with pytest.raises(HTTPException) as error:
        if endpoint == "selected":
            service.selected(session, selection.model_dump())
        elif endpoint == "forms":
            strategy_views.forms(selection, session)
        else:
            strategy_views.create_strategy(strategy_views.CreateStrategy(name="blocked", selections=selection,
                forms={kind: {} for kind in service.KINDS}), session)
    assert error.value.status_code == 422 and error.value.detail["code"] == "version_retired"
    assert session.scalar(select(Strategy)) is None
    inspect.assert_not_called()


@pytest.mark.parametrize("operation", ["forms", "build"])
def test_direct_strategy_service_calls_reject_retired_before_commands_or_directory(tmp_path, monkeypatch, operation):
    components = {kind: {"version_id": None} for kind in service.KINDS}
    components["model"] = {"version_id": str(uuid4()), "scheme_version": "1.1.7"}
    command = MagicMock(side_effect=AssertionError("must not execute"))
    monkeypatch.setattr(service, "command", command)
    with pytest.raises(policy.VersionPolicyError):
        if operation == "forms":
            service.inspect_component(components, "model")
        else:
            service.build(components, {kind: {} for kind in service.KINDS}, tmp_path / "new-strategy")
    assert not (tmp_path / "new-strategy").exists()
    command.assert_not_called()


def test_active_future_series_uses_floor_not_hardcoded_minor(policy_client):
    _, session, root = policy_client
    project = project_record(session, root, scheme="1.3.7", scheme_tag="v1.3.0", template_tag="v1.3.0")
    data = versions.create_version(project.id, versions.CreateVersion(), session)
    assert data["packageVersion"] == "1.3.1"


def test_cli_retired_start_is_blocked_before_scheduler_login(policy_client, monkeypatch):
    from core.scheduler import __main__ as cli

    _, session, root = policy_client
    project = project_record(session, root)
    record, directory = task_record(session, root, project, scheme="1.1.7")
    monkeypatch.setattr(cli, "database_session_factory", lambda: lambda: session)
    client = MagicMock(side_effect=AssertionError("must not log into scheduler"))
    monkeypatch.setattr(cli, "DolphinSchedulerClient", client)
    monkeypatch.setattr(sys, "argv", ["scheduler", "start", "model", "--input-file", str(directory / "input.json")])
    with pytest.raises(HTTPException) as error:
        cli.main()
    assert error.value.status_code == 422 and error.value.detail["code"] == "version_retired"
    client.assert_not_called()


@pytest.mark.parametrize("proof", ["input", "lock", "null", "building", "success", "failed", "submit_failed"])
def test_runtime_requires_persisted_accepted_hashes_and_live_status(policy_client, proof):
    client, session, root = policy_client
    project = project_record(session, root)
    record, directory = task_record(session, root, project, status="queued")
    if proof == "input":
        path = directory / "input.json"
        data = json.loads(path.read_text())
        data["backtest"] = {"tampered": True}
        path.write_text(json.dumps(data))
    elif proof == "lock":
        with (directory / "environment/uv.lock").open("a") as stream:
            stream.write('\n# replaced after acceptance\n')
    elif proof == "null":
        record.input_sha256 = record.lock_sha256 = None
    else:
        record.status = proof
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "task_identity_mismatch"


def strategy_record(session, root):
    import shutil

    project = project_record(session, root)
    saved, source = task_record(session, root, project, status="success", recorded="1.2.0")
    strategy = Strategy(id=uuid4(), name="assembled", status="queued", scheme_version="1.2.0",
                        forms={}, components={"model": {"project_id": str(project.id),
                        "version_id": str(saved.id), "scheme_version": "1.2.0"}})
    directory = root / "runs" / str(strategy.id)
    shutil.copytree(source, directory)
    data = json.loads((directory / "input.json").read_text())
    data.update(kind="strategy", environment={"lockfile": str(directory / "environment/uv.lock")})
    data["algos"]["model"]["wheel"] = str(directory / "candidate.whl")
    (directory / "input.json").write_text(json.dumps(data))
    strategy.input_sha256 = hashes(directory, "strategy")["input_sha256"]
    strategy.lock_sha256 = hashes(directory, "strategy")["lock_sha256"]
    session.add(strategy)
    session.commit()
    return strategy, saved, project, directory


def test_strategy_runtime_binds_selected_saved_result_not_current_workspace(policy_client):
    client, session, root = policy_client
    strategy, saved, project, directory = strategy_record(session, root)
    (policy.project_path(project) / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.1.0"\n')
    endpoint = f"/api/v1/version-policy/tasks/{strategy.id}/check"
    assert client.post(endpoint, json=hashes(directory, "strategy")).json() == {"allowed": True, "grandfathered": False}
    assert saved.scheme_version == "1.2.0"


@pytest.mark.parametrize("identity", ["version_id", "project_id", "entry", "extra", "wheel_escape"])
def test_strategy_runtime_rejects_selected_identity_bypass(policy_client, identity):
    client, session, root = policy_client
    strategy, saved, project, directory = strategy_record(session, root)
    path = directory / "input.json"
    data = json.loads(path.read_text())
    if identity in {"version_id", "project_id"}:
        strategy.components = {"model": {**strategy.components["model"], identity: str(uuid4())}}
    elif identity == "entry":
        data["algos"]["model"]["entry"] = data["algos"]["model"]["entry"].split(":")[0] + ":OtherAlgo"
    elif identity == "extra":
        data["algos"]["optimize"] = dict(data["algos"]["model"])
    else:
        data["algos"]["model"]["wheel"] = str(root / "runs" / str(saved.id) / "candidate.whl")
    path.write_text(json.dumps(data))
    # Even when the exact envelope was persisted, database identity remains binding.
    strategy.input_sha256 = hashes(directory, "strategy")["input_sha256"]
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{strategy.id}/check", json=hashes(directory, "strategy"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "task_identity_mismatch"


def test_retired_rename_blocked_without_environment_or_directory_writes(policy_client, monkeypatch):
    client, session, root = policy_client
    project = project_record(session, root, scheme_commit=SCHEME_OLD)
    original = policy.project_path(project)
    files = {str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()}
    prepare = MagicMock(side_effect=AssertionError("must not rebuild historical environment"))
    monkeypatch.setattr(views, "prepare_environment", prepare)
    url = f"/api/v1/projects/{project.id}"
    response = client.patch(url, json={"name": "renamed"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "version_retired"
    assert project.name == original.name
    assert not original.with_name("renamed").exists()
    assert {str(path.relative_to(original)): path.read_bytes() for path in original.rglob("*") if path.is_file()} == files
    assert client.patch(url, json={"name": project.name, "description": "historical note"}).status_code == 200
    assert project.description == "historical note"
    prepare.assert_not_called()


@pytest.mark.parametrize("version,requirement", [("1.0.0", "scheme>=1.0.0,<2.0.0"),
                                                 ("1.2.99", "scheme>=1.2.0,<2.0.0")])
def test_known_active_unsaved_transitive_wheel_must_obey_complete_minor_contract(policy_client, version, requirement):
    client, session, root = policy_client
    project = project_record(session, root)
    upstream = project_record(session, root, kind="factor")
    record, directory = task_record(session, root, project, status="queued")
    package = f"factor-{upstream.id.hex[:4]}"
    wheel = directory / "factor.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA",
                         f"Name: {package}\nVersion: {version}\nRequires-Dist: {requirement}\n")
        archive.writestr(package.replace("-", "_") + "/__init__.py", "")
    lock = directory / "environment/uv.lock"
    with lock.open("a") as stream:
        stream.write(f'[[package]]\nname="{package}"\nversion="{version}"\nsource={{path="../factor.whl"}}\n')
    record.lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest()
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "task_identity_mismatch"
    assert "主、次版本" in response.json()["detail"]["reason"] or "完整 patch 范围" in response.json()["detail"]["reason"]
