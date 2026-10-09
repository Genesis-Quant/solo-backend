"""Real-policy admission boundaries; all state is in SQLite memory and temp runs."""

import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.artifacts.models import ResearchArtifact
from core.apps.projects import templates, versions, views
from core.apps.projects.models import Project, ProjectVersion
from core.apps.projects.schemas import ProjectCreate, TemplateVersion
from core.apps.strategies import service
from core.apps.strategies import views as strategy_views
from core.apps.strategies.models import Strategy
from core.database.base import Base
from core.database.session import get_database_session
from main import app

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
                status="building", workflow_id=None, recorded=None, accept=True):
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
    record.accepted_artifacts = {package: {
        "id": str(uuid4()), "package": package, "version": record.package_version,
        "sha256": component["sha256"], "entry": component["entry"], "filename": wheel.name,
        "kind": project.kind, "scheme_version": scheme, "sources": policy.project_sources(project),
        "origin_project_id": str(project.id), "dependencies": {},
    }}
    if accept:
        record.input_sha256 = hashlib.sha256((directory / "input.json").read_bytes()).hexdigest()
        record.lock_sha256 = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    session.commit()
    return record, directory


def hashes(directory, kind):
    data = json.loads((directory / "input.json").read_text())
    return {"kind": kind, "input_sha256": hashlib.sha256((directory / "input.json").read_bytes()).hexdigest(),
            "lock_sha256": hashlib.sha256(Path(data["environment"]["lockfile"]).read_bytes()).hexdigest()}


def upstream_capture(session, root, *, source="run", historical=False,
                     source_scheme="1.2.3", source_commit="c" * 40):
    """An unknown consumer copy, with independently owned original evidence."""
    upstream = project_record(session, root, kind="factor", scheme=source_scheme, scheme_commit="d" * 40)
    original, source_directory = task_record(session, root, upstream, scheme=source_scheme,
        scheme_commit=source_commit, status="success", recorded=source_scheme, accept=False)
    original.accepted_artifacts = {}
    if not historical:
        original.input_sha256 = hashes(source_directory, "factor")["input_sha256"]
        original.lock_sha256 = hashes(source_directory, "factor")["lock_sha256"]
    session.commit()
    owned_wheel = source_directory / "candidate.whl"
    package_version = original.package_version
    workspace = policy.project_path(upstream)
    (workspace / ".solo").write_text(json.dumps({
        "project_id": str(upstream.id), "name": upstream.name, "kind": upstream.kind,
    }))
    series = ".".join(source_scheme.split(".")[:2])
    maximum = f"{series.split('.')[0]}.{int(series.split('.')[1]) + 1}.0"
    (workspace / "pyproject.toml").write_text(
        f'[project]\nname="{upstream.package_name}"\nversion="{original.package_version}"\n'
        f'dependencies=["scheme>={series}.0,<{maximum}"]\n'
        f'[tool.uv.sources]\nscheme={{git="https://example.invalid/scheme",rev="{source_commit}"}}\n')
    (workspace / "uv.lock").write_text(
        f'[[package]]\nname="scheme"\nversion="{source_scheme}"\n'
        f'source={{git="https://example.invalid/scheme#{source_commit}"}}\n'
        f'[[package]]\nname="{upstream.package_name}"\nversion="{original.package_version}"\n'
        'source={editable="."}\n')
    if source == "workspace":
        owned_wheel = workspace / "dist" / "original.whl"
        owned_wheel.parent.mkdir()
        shutil.copyfile(source_directory / "candidate.whl", owned_wheel)
        shutil.rmtree(source_directory)
        session.delete(original)
        session.commit()
        original = None
    consumer = project_record(session, root)
    record, directory = task_record(session, root, consumer, scheme="1.2.7", recorded="1.2.7", accept=False)
    record.accepted_artifacts = {}
    shutil.copyfile(owned_wheel, directory / "upstream.whl")
    with (directory / "environment/uv.lock").open("a") as stream:
        stream.write(f'[[package]]\nname="{upstream.package_name}"\nversion="{package_version}"\n'
                     'source={path="../upstream.whl"}\n')
    session.commit()
    return consumer, record, directory, upstream, original, owned_wheel


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
    accepted = client.post(endpoint, json=body).json()
    assert accepted["allowed"] is True and accepted["grandfathered"] is False
    assert accepted["artifacts"] == [{"package": policy.package_name(project), "version": record.package_version,
                                      "sha256": next(iter(record.accepted_artifacts.values()))["sha256"], "wheel": "candidate.whl"}]
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


@pytest.mark.parametrize("invalid", ["retired_actual", "retired_commit", "missing", "malformed",
                                    "duplicate", "contract", "owner", "bytes", "consumer_only"])
def test_capture_unknown_upstream_requires_live_original_source_evidence(policy_client, invalid):
    _, session, root = policy_client
    consumer, record, directory, upstream, _, owned_wheel = upstream_capture(session, root, source="workspace")
    workspace = policy.project_path(upstream)
    lockfile = workspace / "uv.lock"
    if invalid == "retired_actual":
        lockfile.write_text(lockfile.read_text().replace('version="1.2.3"', 'version="1.1.7"'))
    elif invalid == "retired_commit":
        lockfile.write_text(lockfile.read_text().replace("c" * 40, SCHEME_OLD))
    elif invalid == "missing":
        lockfile.unlink()
    elif invalid == "malformed":
        lockfile.write_text("not a lock")
    elif invalid == "duplicate":
        with lockfile.open("a") as stream:
            stream.write('[[package]]\nname="scheme"\nversion="1.2.3"\n')
    elif invalid == "contract":
        config = workspace / "pyproject.toml"
        config.write_text(config.read_text().replace("<1.3.0", "<2.0.0"))
    elif invalid == "owner":
        (workspace / ".solo").write_text(json.dumps({"project_id": str(uuid4()), "name": upstream.name, "kind": upstream.kind}))
    elif invalid == "bytes":
        with ZipFile(owned_wheel, "a") as archive:
            archive.writestr("changed.py", "changed after copy")
    else:
        owned_wheel.unlink()
    digest = hashlib.sha256((directory / "upstream.whl").read_bytes()).hexdigest()
    before = ((directory / "input.json").read_bytes(), (directory / "environment/uv.lock").read_bytes())
    with pytest.raises(ValueError):
        policy.capture_task_artifacts(session, record, project=consumer, directory=directory, kind="model")
    assert session.scalar(select(ResearchArtifact).where(ResearchArtifact.sha256 == digest)) is None
    assert record.input_sha256 is None and record.lock_sha256 is None and not record.accepted_artifacts
    assert ((directory / "input.json").read_bytes(), (directory / "environment/uv.lock").read_bytes()) == before


@pytest.mark.parametrize("source", ["run", "workspace"])
@pytest.mark.parametrize("flat", [False, True])
def test_capture_proven_upstream_keeps_original_scheme_patch_commit_and_runtime_independence(policy_client, source, flat):
    client, session, root = policy_client
    consumer, record, directory, upstream, original, _ = upstream_capture(session, root, source=source)
    source_id = upstream.id
    source_version_id = original.id if original is not None else None
    package = upstream.package_name
    lockfile = directory / "environment/uv.lock"
    if flat:
        registry = directory / "wheels" / package
        registry.mkdir(parents=True)
        wheel = registry / "original.whl"
        shutil.move(directory / "upstream.whl", wheel)
        lockfile.write_text(lockfile.read_text().replace('source={path="../upstream.whl"}',
            f'source={{registry="../wheels/{package}"}}\nwheels=[{{path="{wheel.name}",'
            f'hash="sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}"}}]'))
    if original is not None:
        # The original frozen run, not its now-retired workspace, proves this wheel.
        (policy.project_path(upstream) / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.1.7"\n')
    captured = policy.capture_task_artifacts(session, record, project=consumer, directory=directory, kind="model")
    snapshot = captured[package]
    assert snapshot["scheme_version"] == "1.2.3"
    assert snapshot["sources"]["scheme"]["version"] == "1.2.3"
    assert snapshot["sources"]["scheme"]["commit"] == "c" * 40
    assert snapshot["origin_project_id"] == str(source_id)
    assert snapshot["origin_version_id"] == (str(source_version_id) if source_version_id else None)
    record.accepted_artifacts = captured
    record.input_sha256 = hashes(directory, "model")["input_sha256"]
    record.lock_sha256 = hashes(directory, "model")["lock_sha256"]
    record.status = "queued"
    session.commit()
    if original is not None:
        shutil.rmtree(policy.run_path(original.id))
    shutil.rmtree(policy.project_path(upstream))
    session.delete(upstream)
    session.commit()
    before = ((directory / "input.json").read_bytes(), lockfile.read_bytes())
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 200, response.text
    assert next(item for item in response.json()["artifacts"] if item["package"] == package)["sha256"] == snapshot["sha256"]
    assert record.scheme_version == "1.2.7" and record.accepted_artifacts == captured
    assert ((directory / "input.json").read_bytes(), lockfile.read_bytes()) == before


@pytest.mark.parametrize("invalid", ["input_hash", "lock_hash", "missing_hashes", "wheel", "component", "scheme",
                                    "lock_wheel", "lock_version", "lock_package", "contract"])
def test_capture_unknown_upstream_frozen_source_proof_cannot_be_replaced_by_consumer(policy_client, invalid):
    _, session, root = policy_client
    consumer, record, directory, upstream, original, owned_wheel = upstream_capture(session, root)
    if invalid == "input_hash":
        with (policy.run_path(original.id) / "input.json").open("a") as stream:
            stream.write("\n")
    elif invalid == "lock_hash":
        with (policy.run_path(original.id) / "environment/uv.lock").open("a") as stream:
            stream.write("\n")
    elif invalid == "missing_hashes":
        original.input_sha256 = original.lock_sha256 = None
    elif invalid == "wheel":
        owned_wheel.unlink()
    elif invalid == "component":
        data = json.loads((policy.run_path(original.id) / "input.json").read_text())
        data["factor"]["sha256"] = "0" * 64
        (policy.run_path(original.id) / "input.json").write_text(json.dumps(data))
        original.input_sha256 = hashes(policy.run_path(original.id), "factor")["input_sha256"]
    elif invalid == "scheme":
        original.scheme_version = "1.2.7"
    elif invalid.startswith("lock_"):
        lockfile = policy.run_path(original.id) / "environment/uv.lock"
        if invalid == "lock_wheel":
            shutil.copyfile(owned_wheel, owned_wheel.with_name("other.whl"))
            lockfile.write_text(lockfile.read_text().replace("../candidate.whl", "../other.whl"))
        elif invalid == "lock_version":
            lockfile.write_text(lockfile.read_text().replace(f'version="{original.package_version}"', 'version="1.2.99"'))
        else:
            lockfile.write_text(lockfile.read_text().replace(upstream.package_name, "factor-abcd"))
        original.lock_sha256 = hashlib.sha256(lockfile.read_bytes()).hexdigest()
    else:
        with ZipFile(owned_wheel, "w") as archive:
            archive.writestr(f"{upstream.package_name}.dist-info/METADATA",
                f"Name: {upstream.package_name}\nVersion: {original.package_version}\nRequires-Dist: scheme>=1.2.0,<2.0.0\n")
            archive.writestr(upstream.package_name.replace("-", "_") + "/__init__.py", "")
        digest = hashlib.sha256(owned_wheel.read_bytes()).hexdigest()
        data = json.loads((policy.run_path(original.id) / "input.json").read_text())
        data["factor"]["sha256"] = digest
        (policy.run_path(original.id) / "input.json").write_text(json.dumps(data))
        original.input_sha256 = hashes(policy.run_path(original.id), "factor")["input_sha256"]
        shutil.copyfile(owned_wheel, directory / "upstream.whl")
    session.commit()
    digest = hashlib.sha256((directory / "upstream.whl").read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        policy.capture_task_artifacts(session, record, project=consumer, kind="model")
    assert session.scalar(select(ResearchArtifact).where(ResearchArtifact.sha256 == digest)) is None
    assert not record.accepted_artifacts


@pytest.mark.parametrize("hint", [False, True])
def test_capture_known_upstream_digest_does_not_recheck_deleted_original_source(policy_client, hint):
    _, session, root = policy_client
    consumer, record, directory, upstream, original, _ = upstream_capture(session, root)
    captured = policy.capture_task_artifacts(session, record, project=consumer, kind="model")
    if hint:
        consumer.artifact_dependencies = {upstream.package_name: captured[upstream.package_name]}
    shutil.rmtree(policy.run_path(original.id))
    shutil.rmtree(policy.project_path(upstream))
    session.delete(upstream)
    session.commit()
    assert policy.capture_task_artifacts(session, record, project=consumer, kind="model") == captured


@pytest.mark.parametrize("chosen", ["1.2.1", "1.2.2"])
def test_capture_mixed_upstream_package_patches_keeps_exact_registered_selection(policy_client, chosen):
    client, session, root = policy_client
    model, model_run, model_directory, upstream, first, _ = upstream_capture(session, root)
    model_run.accepted_artifacts = policy.capture_task_artifacts(session, model_run, project=model, kind="model")
    second, second_directory = task_record(session, root, upstream, scheme="1.2.0", scheme_commit="e" * 40,
                                           status="success", recorded="1.2.0", accept=False)
    second.accepted_artifacts = policy.capture_task_artifacts(session, second, project=upstream, kind="factor")
    optimize = project_record(session, root, kind="optimize")
    optimize_run, optimize_directory = task_record(session, root, optimize, recorded="1.2.0", accept=False)
    optimize_run.accepted_artifacts = {}
    shutil.copyfile(second_directory / "candidate.whl", optimize_directory / "upstream.whl")
    with (optimize_directory / "environment/uv.lock").open("a") as stream:
        stream.write(f'[[package]]\nname="{upstream.package_name}"\nversion="{second.package_version}"\n'
                     'source={path="../upstream.whl"}\n')
    optimize_run.accepted_artifacts = policy.capture_task_artifacts(session, optimize_run, project=optimize, kind="optimize")
    strategy = Strategy(id=uuid4(), name="mixed exact patches", status="queued", scheme_version="1.2.7", forms={},
        components={stage: {"project_id": str(project.id), "version_id": str(saved.id)}
                    for stage, project, saved in (("model", model, model_run), ("optimize", optimize, optimize_run))})
    session.add(strategy)
    session.commit()
    directory = policy.run_path(strategy.id)
    (directory / "environment").mkdir(parents=True)
    algos = {}
    packages = ['[[package]]\nname="scheme"\nversion="1.2.7"\n'
                f'source={{git="https://example.invalid/scheme#{"b" * 40}"}}\n']
    for stage, saved, source in (("model", model_run, model_directory), ("optimize", optimize_run, optimize_directory)):
        component = json.loads((source / "input.json").read_text())["algos"][stage]
        wheel = directory / f"{stage}.whl"
        shutil.copyfile(Path(component["wheel"]), wheel)
        algos[stage] = {**component, "wheel": str(wheel)}
        packages.append(f'[[package]]\nname="{component["package"]}"\nversion="{component["version"]}"\n'
                        f'source={{path="../{wheel.name}"}}\n')
    registry = directory / "wheels" / upstream.package_name
    registry.mkdir(parents=True)
    for saved in (first, second):
        shutil.copyfile(policy.run_path(saved.id) / "candidate.whl", registry / f"{saved.package_version}.whl")
    expected = (model_run if chosen == "1.2.1" else optimize_run).accepted_artifacts[upstream.package_name]
    packages.append(f'[[package]]\nname="{upstream.package_name}"\nversion="{chosen}"\n'
                    f'source={{registry="../wheels/{upstream.package_name}"}}\n'
                    f'wheels=[{{path="{chosen}.whl",hash="sha256:{expected["sha256"]}"}}]\n')
    (directory / "environment/uv.lock").write_text("".join(packages))
    (directory / "input.json").write_text(json.dumps({"kind": "strategy", "algos": algos,
        "environment": {"lockfile": str(directory / "environment/uv.lock")}, "backtest": {}}))
    shutil.rmtree(policy.project_path(upstream))
    for saved in (first, second):
        shutil.rmtree(policy.run_path(saved.id))
    session.delete(upstream)
    session.commit()
    strategy.accepted_artifacts = policy.capture_task_artifacts(session, strategy, kind="strategy")
    assert strategy.accepted_artifacts[expected["package"]] == expected
    strategy.input_sha256 = hashes(directory, "strategy")["input_sha256"]
    strategy.lock_sha256 = hashes(directory, "strategy")["lock_sha256"]
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{strategy.id}/check", json=hashes(directory, "strategy"))
    assert response.status_code == 200, response.text
    assert next(item for item in response.json()["artifacts"] if item["package"] == expected["package"])["sha256"] == expected["sha256"]


def test_historical_upstream_capture_preserves_null_hashes_and_original_retired_scheme(policy_client):
    _, session, root = policy_client
    consumer, record, directory, upstream, original, _ = upstream_capture(session, root,
        historical=True, source_scheme="1.1.7", source_commit=SCHEME_OLD)
    before = {path: path.read_bytes() for run in (directory, policy.run_path(original.id))
              for path in (run / "input.json", run / "environment/uv.lock")}
    captured = policy.capture_task_artifacts(session, record, project=consumer, kind="model", historical=True)
    record.accepted_artifacts = captured
    session.commit()
    assert captured[upstream.package_name]["scheme_version"] == "1.1.7"
    assert captured[upstream.package_name]["sources"]["scheme"]["commit"] == SCHEME_OLD
    assert original.input_sha256 is None and original.lock_sha256 is None
    assert record.input_sha256 is None and record.lock_sha256 is None
    assert {path: path.read_bytes() for path in before} == before


def test_known_upstream_template_source_cannot_bypass_retirement(policy_client):
    client, session, root = policy_client
    project = project_record(session, root)
    upstream = project_record(session, root, kind="factor", template_commit=TEMPLATE_OLD)
    record, directory = task_record(session, root, project, status="queued")
    lock = directory / "environment/uv.lock"
    package = policy.package_name(upstream)
    wheel = directory / "upstream.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA", f"Name: {package}\nVersion: 1.2.99\nRequires-Dist: scheme>=1.2.0,<1.3.0\n")
        archive.writestr(package.replace("-", "_") + "/__init__.py", "")
    with lock.open("a") as stream:
        stream.write(f'[[package]]\nname="{package}"\nversion="1.2.99"\nsource={{path="../upstream.whl"}}\n')
    record.input_sha256 = record.lock_sha256 = None
    session.commit()
    record.accepted_artifacts = {**record.accepted_artifacts, package: {
        "package": package, "version": "1.2.99", "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "entry": f"{package.replace('-', '_')}:Factor", "kind": "factor", "sources": policy.project_sources(upstream),
    }}
    record.input_sha256 = hashlib.sha256((directory / "input.json").read_bytes()).hexdigest()
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
    strategy.accepted_artifacts = dict(saved.accepted_artifacts)
    strategy.components = {"model": {**strategy.components["model"], "component_snapshot": {
        key: data["algos"]["model"][key] for key in ("package", "version", "entry", "sha256")
    }}}
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
    accepted = client.post(endpoint, json=hashes(directory, "strategy")).json()
    assert accepted["allowed"] is True and accepted["grandfathered"] is False
    assert len(accepted["artifacts"]) == 1
    assert saved.scheme_version == "1.2.0"


@pytest.mark.parametrize("identity", ["snapshot", "entry", "extra", "wheel_escape"])
def test_strategy_runtime_rejects_selected_identity_bypass(policy_client, identity):
    client, session, root = policy_client
    strategy, saved, project, directory = strategy_record(session, root)
    path = directory / "input.json"
    data = json.loads(path.read_text())
    if identity == "snapshot":
        selection = strategy.components["model"]
        strategy.components = {"model": {**selection, "component_snapshot": {**selection["component_snapshot"], "sha256": "0" * 64}}}
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


def test_strategy_frozen_identity_survives_source_records_and_run_removal(policy_client):
    import shutil

    client, session, root = policy_client
    strategy, saved, project, directory = strategy_record(session, root)
    envelope = (directory / "input.json").read_bytes()
    lock = (directory / "environment/uv.lock").read_bytes()
    shutil.rmtree(root / "runs" / str(saved.id))
    shutil.rmtree(policy.project_path(project))
    session.delete(project)
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{strategy.id}/check", json=hashes(directory, "strategy"))
    assert response.status_code == 200, response.text
    assert response.json()["allowed"] is True
    assert len(response.json()["artifacts"]) == 1
    assert (directory / "input.json").read_bytes() == envelope
    assert (directory / "environment/uv.lock").read_bytes() == lock


def test_strategy_origin_ids_are_traceability_not_runtime_foreign_keys(policy_client):
    client, session, root = policy_client
    strategy, _, _, directory = strategy_record(session, root)
    strategy.components = {"model": {**strategy.components["model"], "project_id": str(uuid4()), "version_id": str(uuid4())}}
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{strategy.id}/check", json=hashes(directory, "strategy"))
    assert response.status_code == 200, response.text


def test_runtime_missing_snapshot_is_not_rederived_from_live_origin(policy_client):
    client, session, root = policy_client
    project = project_record(session, root)
    record, directory = task_record(session, root, project, status="queued", accept=False)
    record.accepted_artifacts = None
    record.input_sha256 = hashes(directory, "model")["input_sha256"]
    record.lock_sha256 = hashes(directory, "model")["lock_sha256"]
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 422
    assert "产物快照" in response.json()["detail"]["reason"]
    assert record.accepted_artifacts is None


def test_transitive_wheel_hash_is_bound_independently_of_lockfile_hash(policy_client):
    client, session, root = policy_client
    project = project_record(session, root)
    record, directory = task_record(session, root, project, status="queued", accept=False)
    package = "factor-abcd"
    wheel = directory / "factor.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA", "Name: factor-abcd\nVersion: 1.2.9\nRequires-Dist: scheme>=1.2.0,<1.3.0\n")
        archive.writestr("factor_abcd/__init__.py", "")
    with (directory / "environment/uv.lock").open("a") as stream:
        stream.write('[[package]]\nname="factor-abcd"\nversion="1.2.9"\nsource={path="../factor.whl"}\n')
    record.accepted_artifacts = {**record.accepted_artifacts, package: {
        "package": package, "version": "1.2.9", "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "entry": "factor_abcd:Factor", "kind": "factor", "sources": policy.project_sources(project),
    }}
    record.input_sha256 = hashes(directory, "model")["input_sha256"]
    record.lock_sha256 = hashes(directory, "model")["lock_sha256"]
    session.commit()
    endpoint = f"/api/v1/version-policy/tasks/{record.id}/check"
    assert client.post(endpoint, json=hashes(directory, "model")).status_code == 200
    with ZipFile(wheel, "a") as archive:
        archive.writestr("factor_abcd/tampered.py", "changed")
    response = client.post(endpoint, json=hashes(directory, "model"))
    assert response.status_code == 422
    assert "准确产物" in response.json()["detail"]["reason"]


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
    record, directory = task_record(session, root, project, status="queued", accept=False)
    package = f"factor-{upstream.id.hex[:4]}"
    wheel = directory / "factor.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA",
                         f"Name: {package}\nVersion: {version}\nRequires-Dist: {requirement}\n")
        archive.writestr(package.replace("-", "_") + "/__init__.py", "")
    lock = directory / "environment/uv.lock"
    with lock.open("a") as stream:
        stream.write(f'[[package]]\nname="{package}"\nversion="{version}"\nsource={{path="../factor.whl"}}\n')
    record.accepted_artifacts = {**record.accepted_artifacts, package: {
        "package": package, "version": version, "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "entry": f"{package.replace('-', '_')}:Factor", "kind": "factor", "sources": policy.project_sources(upstream),
    }}
    record.input_sha256 = hashes(directory, "model")["input_sha256"]
    record.lock_sha256 = hashlib.sha256(lock.read_bytes()).hexdigest()
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json=hashes(directory, "model"))
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "task_identity_mismatch"
    assert "主、次版本" in response.json()["detail"]["reason"] or "完整 patch 范围" in response.json()["detail"]["reason"]


@pytest.mark.parametrize("tag,actual,allowed", [
    ("stable-current", "1.2.0", True),
    ("v1.1.42", "1.2.0", True),
    ("stable-current", None, False),
    ("v1.2.0", None, True),
    ("stable-current", "1.1.42", False),
])
def test_project_template_creation_version_is_offline_and_legacy_custom_fails_closed(policy_client, monkeypatch, tag, actual, allowed):
    client, session, root = policy_client
    project = project_record(session, root, scheme_tag="v1.1.42", template_tag=tag)
    project.template_package_version = actual
    session.commit()
    before = project.updated_at
    remote = MagicMock(side_effect=AssertionError("must not fetch metadata on historical reads/check"))
    monkeypatch.setattr(templates, "project_metadata", remote)
    response = client.get(f"/api/v1/projects/{project.id}")
    assert response.status_code == 200
    assert response.json()["retired"] is not allowed
    check = client.post(f"/api/v1/version-policy/projects/{project.id}/check")
    assert check.status_code == (200 if allowed else 422), check.text
    if actual is None and tag == "stable-current":
        assert check.json()["detail"]["code"] == "invalid_version_source"
    assert project.template_package_version == actual
    assert project.updated_at == before
    remote.assert_not_called()


@pytest.mark.parametrize("source", ["scheme_tag", "scheme_commit", "template_tag", "template_commit"])
def test_persisted_active_template_actual_never_overrides_retired_identity(policy_client, source):
    client, session, root = policy_client
    kwargs = {
        "scheme_tag": {"scheme_tag": "v1.1.0"},
        "scheme_commit": {"scheme_commit": SCHEME_OLD},
        "template_tag": {"template_tag": "v1.0.1"},
        "template_commit": {"template_commit": TEMPLATE_OLD},
    }[source]
    project = project_record(session, root, **kwargs)
    project.template_package_version = "1.2.0"
    session.commit()
    response = client.post(f"/api/v1/version-policy/projects/{project.id}/check")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "version_retired"
