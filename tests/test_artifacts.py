"""Source-independent registry/publication using only SQLite and temporary shared files."""

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
import tomlkit
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.artifacts.backfill import backfill_tasks
from core.apps.artifacts.models import ArtifactInstallation, ResearchArtifact
from core.apps.artifacts.service import (ArtifactError, artifact_component, artifact_path, artifact_snapshot,
    gc_unreferenced, publish_version, register_asset, register_file, require_artifact, verify_bundle)
from core.apps.projects.models import Project, ProjectDeletion, ProjectVersion
from core.apps.strategies.models import Strategy
from core.database.base import Base
from core.database.session import get_database_session
from main import app


@pytest.fixture
def registry(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, record):
        connection.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    # TestClient lifespan must never construct/dispose the configured DB engine.
    monkeypatch.setattr("main.database_engine", lambda: SimpleNamespace(dispose=lambda: None))
    session = Session(engine, expire_on_commit=False)
    app.dependency_overrides[get_database_session] = lambda: session
    with TestClient(app) as client:
        yield session, tmp_path, client
    app.dependency_overrides.clear()
    session.close()
    engine.dispose()


def make_project(session, root, *, kind="model", name=None, scheme="1.2.0", template="1.2.0"):
    project = Project(id=uuid4(), name=name or str(uuid4()), description="", kind=kind,
        template_tag="v" + template, template_commit="a" * 40, template_package_version=template,
        scheme_version="v" + scheme, scheme_commit="b" * 40)
    session.add(project)
    session.commit()
    directory = root / "projects" / kind / project.name
    directory.mkdir(parents=True)
    (directory / ".solo").write_text(json.dumps({"project_id": str(project.id), "name": project.name, "kind": kind}))
    (directory / "uv.lock").write_text(tomlkit.dumps({"package": [{"name": "scheme", "version": scheme}]}))
    return project


def make_wheel(path, package, version="1.2.1", *, contents="", scheme="1.2.0", requires_scheme=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    module = package.replace("-", "_")
    requirements = f"Requires-Dist: scheme>={'.'.join(scheme.split('.')[:2])}.0,<{scheme.split('.')[0]}.{int(scheme.split('.')[1]) + 1}.0\n" if requires_scheme else ""
    with ZipFile(path, "w") as archive:
        metadata = f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n{requirements}\n"
        archive.writestr(f"{module}-{version}.dist-info/METADATA", metadata)
        archive.writestr(f"{module}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nGenerator: tests\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{module}-{version}.dist-info/RECORD", "")
        archive.writestr(f"{module}/__init__.py", contents)
    return path


def make_run(session, root, project, *, assets=None, flat=False, accepted=True, scheme="1.2.0"):
    number = (session.scalar(select(ProjectVersion.number).where(ProjectVersion.project_id == project.id).order_by(ProjectVersion.number.desc())) or 0) + 1
    version = ProjectVersion(id=uuid4(), project_id=project.id, number=number, package_version=f"{'.'.join(scheme.split('.')[:2])}.{number}",
        status="success", scheme_version=scheme, parameters={})
    session.add(version)
    session.commit()
    directory = root / "runs" / str(version.id)
    environment = directory / "environment"
    environment.mkdir(parents=True)
    wheel = make_wheel(directory / "wheels" / f"{project.package_name.replace('-', '_')}-{version.package_version}-py3-none-any.whl",
        project.package_name, version.package_version, scheme=scheme)
    component = {"package": project.package_name, "version": version.package_version,
        "entry": f"{project.package_name.replace('-', '_')}:{'Factor' if project.kind == 'factor' else 'Algo'}",
        "wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    packages = [{"name": "scheme", "version": scheme, "source": {"git": "https://example.invalid/scheme#" + "b" * 40}},
                {"name": project.package_name, "version": version.package_version, "source": {"path": str(wheel)},
                 "wheels": [{"filename": wheel.name}]}]
    sources = {project.package_name: {"path": str(wheel)}, "scheme": {"git": "https://example.invalid/scheme", "rev": "b" * 40}}
    indexes = [{"name": "private", "url": "https://private.example.invalid/simple", "default": True, "explicit": False}]
    for name, source in (assets or {}).items():
        copied = directory / "wheels" / name / source.name
        copied.parent.mkdir(parents=True)
        shutil.copyfile(source, copied)
        from core.apps.artifacts.service import wheel_metadata
        _, package_version, _, _ = wheel_metadata(copied)
        if flat:
            packages.append({"name": name, "version": package_version, "source": {"registry": copied.parent.as_posix()},
                             "wheels": [{"path": copied.name, "hash": "sha256:" + hashlib.sha256(copied.read_bytes()).hexdigest()}]})
            sources[name] = {"index": "local-" + name}
            indexes.append({"name": "local-" + name, "url": str(copied.parent), "format": "flat"})
        else:
            packages.append({"name": name, "version": package_version, "source": {"path": str(copied)}, "wheels": [{"filename": copied.name}]})
            sources[name] = {"path": str(copied)}
    (environment / "uv.lock").write_text(tomlkit.dumps({"version": 1, "package": packages}), encoding="utf-8")
    (environment / "pyproject.toml").write_text(tomlkit.dumps({"project": {"name": "research-environment", "version": "0.0.0", "requires-python": ">=3.12",
        "dependencies": [project.package_name + "==" + version.package_version]}, "tool": {"uv": {"package": False, "sources": sources,
        "index": indexes, "index-strategy": "first-index"}}}), encoding="utf-8")
    data = {"kind": project.kind, "environment": {"lockfile": str(environment / "uv.lock")}, "output": str(directory / "report")}
    if project.kind == "factor":
        data.update(factor=component, analysis={})
    else:
        data.update(algos={project.kind: component}, backtest={"capital": 1000})
    (directory / "input.json").write_text(json.dumps(data), encoding="utf-8")
    version.input_sha256 = hashlib.sha256((directory / "input.json").read_bytes()).hexdigest()
    version.lock_sha256 = hashlib.sha256((environment / "uv.lock").read_bytes()).hexdigest()
    (directory / "report").mkdir()
    (directory / "report" / "run.json").write_text(json.dumps({"status": "success", "input_sha256": version.input_sha256,
        "lock_sha256": version.lock_sha256, "versions": {"scheme": scheme}}))
    if accepted:
        version.accepted_artifacts = policy.capture_task_artifacts(session, version, project=project, kind=project.kind)
    session.commit()
    return version, directory, component


def test_schema_defaults_cascade_and_origin_independence(registry):
    session, root, _ = registry
    project = make_project(session, root)
    version, directory, component = make_run(session, root, project)
    artifact = require_artifact(session, version.accepted_artifacts[project.package_name])
    assert project.package_name == f"model-{project.id.hex[:4]}"
    assert project.artifact_dependencies == {}
    assert inspect(session.bind).get_foreign_keys("project_versions")[0]["options"]["ondelete"] == "CASCADE"
    assert inspect(session.bind).get_foreign_keys("research_artifacts") == []
    source_id, version_id = project.id, version.id
    session.execute(delete(Project).where(Project.id == project.id))
    session.commit()
    session.expire_all()
    assert session.get(ProjectVersion, version_id) is None
    assert session.get(ResearchArtifact, artifact.id).origin_project_id == source_id
    assert require_artifact(session, artifact.id).origin_version_id == version_id
    job = ProjectDeletion(id=source_id, project_name=project.name, kind=project.kind)
    session.add(job)
    session.commit()
    assert job.payload == {} and job.delete_files is True and job.error == ""


def test_registration_digest_identity_entry_and_sources_are_immutable(registry):
    session, root, _ = registry
    project = make_project(session, root)
    wheel = make_wheel(root / "projects" / project.kind / project.name / "candidate.whl", project.package_name)
    component = {"entry": f"{project.package_name.replace('-', '_')}:Algo"}
    artifact = register_file(session, wheel, project, component=component, scheme_version="1.2.0")
    session.commit()
    snapshot = artifact_snapshot(artifact)
    assert artifact.published_at is None
    assert register_file(session, wheel, project, component=component, scheme_version="1.2.0") is artifact
    with pytest.raises(ArtifactError, match="sha256"):
        register_file(session, wheel, project, component={**component, "sha256": "0" * 64}, scheme_version="1.2.0")
    with pytest.raises(ArtifactError, match="entry"):
        register_file(session, wheel, project, component={"entry": component["entry"].replace(":Algo", ":Different")}, scheme_version="1.2.0")
    with pytest.raises(ArtifactError, match="explicit source Project"):
        register_file(session, wheel, make_project(session, root), component=component, scheme_version="1.2.0")
    modified = deepcopy(snapshot)
    modified["sources"]["template"]["commit"] = "c" * 40
    with pytest.raises(ArtifactError, match="sources"):
        require_artifact(session, modified)
    artifact.sources = modified["sources"]
    with pytest.raises(ValueError, match="immutable"):
        session.flush()
    session.rollback()
    assert artifact_snapshot(artifact) == snapshot
    # Mutable Project provenance changes cannot rewrite the frozen origin.
    project.template_commit = "c" * 40
    session.commit()
    assert register_file(session, wheel, project, component=component, scheme_version="1.2.0").sources == snapshot["sources"]


def test_task_snapshots_cannot_change_after_persisted_hash_acceptance(registry):
    session, root, _ = registry
    project = make_project(session, root)
    version, directory, component = make_run(session, root, project)
    snapshots = deepcopy(version.accepted_artifacts)
    version.accepted_artifacts = {project.package_name: {**snapshots[project.package_name], "sources": {}}}
    version.input_sha256 = version.lock_sha256 = None
    with pytest.raises(ValueError, match="immutable after exact hash acceptance"):
        session.flush()
    session.rollback()
    assert version.accepted_artifacts == snapshots
    strategy = Strategy(name="accepted", forms={}, components={}, accepted_artifacts=snapshots,
                        input_sha256="a" * 64, lock_sha256="b" * 64)
    session.add(strategy)
    session.commit()
    strategy.accepted_artifacts = {}
    with pytest.raises(ValueError, match="immutable after exact hash acceptance"):
        session.flush()
    session.rollback()
    assert strategy.accepted_artifacts == snapshots


def test_unpublished_same_package_version_allows_different_bytes(registry):
    session, root, _ = registry
    project = make_project(session, root)
    directory = root / "projects" / project.kind / project.name
    first = register_file(session, make_wheel(directory / "a.whl", project.package_name, contents="a=1"), project, scheme_version="1.2.0")
    second = register_file(session, make_wheel(directory / "b.whl", project.package_name, contents="a=2"), project, scheme_version="1.2.0")
    session.commit()
    assert first.id != second.id and first.version == second.version
    assert len(session.scalars(select(ResearchArtifact)).all()) == 2


def test_publication_http_survives_source_database_and_files_deletion(registry):
    session, root, client = registry
    project = make_project(session, root)
    version, directory, component = make_run(session, root, project)
    originals = {"input": (directory / "input.json").read_bytes(), "lock": (directory / "environment/uv.lock").read_bytes()}
    endpoint = f"/api/v1/projects/{project.id}/versions/{version.id}/publish"
    response = client.post(endpoint, json={})
    assert response.status_code == 200, response.text
    artifact_id = response.json()["artifactId"]
    assert response.json()["publishStatus"] == "published"
    assert client.post(endpoint, json={}).json()["artifactId"] == artifact_id
    artifact = session.get(ResearchArtifact, UUID(artifact_id))
    bundle, data = verify_bundle(artifact)
    assert (bundle / "original/input.json").read_bytes() == originals["input"]
    assert (bundle / "original/uv.lock").read_bytes() == originals["lock"]
    assert (directory / "input.json").read_bytes() == originals["input"]
    assert (directory / "environment/uv.lock").read_bytes() == originals["lock"]
    expected = Path(component["wheel"]).read_bytes()
    name, source_id, saved_id = project.name, project.id, version.id
    session.delete(project)
    session.commit()
    shutil.rmtree(directory)
    shutil.rmtree(root / "projects" / "model" / name)
    session.expire_all()
    assert session.get(Project, source_id) is None and session.get(ProjectVersion, saved_id) is None
    listing = client.get("/api/v1/artifacts?published=true&kind=model").json()
    assert len(listing) == 1 and listing[0]["sourceProjectId"] == str(source_id)
    assert listing[0]["sizeBytes"] == len(expected) and listing[0]["sources"]["scheme"]["version"] == "1.2.0"
    assert client.get(f"/api/v1/artifacts/{artifact_id}/wheel").content == expected
    selected = artifact_component(session, artifact.id)
    assert selected["version_id"] is None and selected["artifact_id"] == artifact_id and selected["kind"] == "model"
    assert selected["input"]["backtest"] == {"capital": 1000}
    assert Path(selected["input"]["environment"]["lockfile"]).is_file()
    assert Path(selected["component"]["wheel"]) == artifact_path(artifact)
    assert gc_unreferenced(session)["removed"] == 0


def test_publication_copies_nonresearch_and_flat_assets_preserves_registry_policy(registry):
    session, root, _ = registry
    project = make_project(session, root)
    dependency = make_wheel(root / "assets" / "helper-2.0.1-py3-none-any.whl", "helper", "2.0.1", requires_scheme=False)
    version, directory, component = make_run(session, root, project, assets={"helper": dependency}, flat=True)
    (directory / "environment/.venv").mkdir()
    (directory / "environment/.venv/never-copy").write_text("virtual environment")
    before = {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()}
    artifact = publish_version(session, project, version)
    session.commit()
    bundle, data = verify_bundle(artifact)
    assert "helper" in artifact.dependencies and artifact.dependencies["helper"]["kind"] == "dependency"
    copied = bundle / "wheels" / "helper" / dependency.name
    assert copied.read_bytes() == dependency.read_bytes()
    assert not (bundle / "environment/.venv").exists()
    config = tomllib.loads((bundle / "environment/pyproject.toml").read_text())
    assert config["tool"]["uv"]["index"] == [{"name": "private", "url": "https://private.example.invalid/simple", "default": True, "explicit": False}]
    assert config["tool"]["uv"]["index-strategy"] == "first-index"
    assert config["tool"]["uv"]["sources"]["helper"] == {"path": str(copied)}
    lock = tomllib.loads((bundle / "environment/uv.lock").read_text())
    locked = next(item for item in lock["package"] if item["name"] == "helper")
    assert locked["source"] == {"path": str(copied)} and locked["version"] == "2.0.1"
    assert {str(path): path.read_bytes() for path in directory.rglob("*") if path.is_file()} == before
    shutil.rmtree(directory)
    require_artifact(session, artifact.dependencies["helper"])
    artifact_component(session, artifact.id)


@pytest.mark.parametrize("missing", ["accepted", "input", "lock", "local_asset", "registry_wheel", "environment", "report"])
def test_publication_missing_sources_fail_closed(registry, missing):
    session, root, _ = registry
    project = make_project(session, root)
    version, directory, component = make_run(session, root, project)
    artifact = require_artifact(session, version.accepted_artifacts[project.package_name])
    if missing == "accepted":
        version.accepted_artifacts = {}
    elif missing == "input":
        version.input_sha256 = None
    elif missing == "lock":
        version.lock_sha256 = None
    else:
        file = {"local_asset": Path(component["wheel"]), "registry_wheel": artifact_path(artifact),
                "environment": directory / "environment/pyproject.toml", "report": directory / "report/run.json"}[missing]
        file.unlink()
    with pytest.raises((ValueError, OSError)):
        publish_version(session, project, version)
    assert artifact.published_at is None


def test_download_and_bundle_tampering_are_denied(registry):
    session, root, client = registry
    project = make_project(session, root)
    version, directory, component = make_run(session, root, project)
    artifact = publish_version(session, project, version)
    session.commit()
    bundle, data = verify_bundle(artifact)
    (bundle / "input.json").write_text('{"tampered":true}')
    with pytest.raises(ArtifactError, match="changed after publication"):
        artifact_component(session, artifact.id)
    artifact_path(artifact).write_bytes(b"replaced wheel")
    response = client.get(f"/api/v1/artifacts/{artifact.id}/wheel")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "artifact_tampered"


def test_local_register_api_requires_digest_owner_and_consumer_assets(registry):
    session, root, client = registry
    source, consumer = make_project(session, root, kind="factor"), make_project(session, root)
    wheel = make_wheel(root / "projects" / consumer.kind / consumer.name / ".solo-wheels" / "factor.whl", source.package_name)
    install_id = str(uuid4())
    begun = client.post(f"/api/v1/projects/{consumer.id}/dependencies/installations",
                        json={"install_id": install_id})
    assert begun.status_code == 200, begun.text
    body = {"project_id": str(source.id), "consumer_project_id": str(consumer.id), "install_id": install_id,
            "wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    unprotected = {key: value for key, value in body.items() if key != "install_id"}
    assert client.post("/api/v1/artifacts/register", json=unprotected).status_code == 409
    assert client.post("/api/v1/artifacts/register", json={**body, "sha256": "0" * 64}).status_code == 422
    assert client.post("/api/v1/artifacts/register", json={**body, "project_id": str(consumer.id)}).status_code == 422
    outside = root / "temporary.whl"
    shutil.copyfile(wheel, outside)
    assert client.post("/api/v1/artifacts/register", json={**body, "wheel": str(outside)}).status_code == 422
    result = client.post("/api/v1/artifacts/register", json=body)
    assert result.status_code == 201, result.text
    assert result.json()["publishedAt"] is None
    digest = result.json()["sha256"]
    assert client.get("/api/v1/artifacts", params={"sha256": digest}).json()[0]["id"] == result.json()["id"]
    session.delete(source)
    session.commit()
    assert client.post("/api/v1/artifacts/register", json=body).status_code == 201


def test_dependency_acceptance_persists_installed_exact_registry_snapshots(registry):
    session, root, client = registry
    source, consumer = make_project(session, root, kind="factor"), make_project(session, root)
    directory = root / "projects" / consumer.kind / consumer.name
    wheel = make_wheel(directory / ".solo-wheels/factor.whl", source.package_name)
    artifact = register_file(session, wheel, source, scheme_version="1.2.0")
    session.commit()
    (directory / "uv.lock").write_text(tomlkit.dumps({"package": [{"name": "scheme", "version": "1.2.0"},
        {"name": source.package_name, "version": "1.2.1", "source": {"path": str(wheel)}}]}))
    url = f"/api/v1/projects/{consumer.id}/dependencies"
    assert client.post(url, json={"artifacts": []}).status_code == 422
    response = client.post(url, json={"artifacts": [str(artifact.id)]})
    assert response.status_code == 200, response.text
    assert response.json() == {"artifacts": [artifact_snapshot(artifact)]}
    assert consumer.artifact_dependencies[source.package_name] == artifact_snapshot(artifact)
    previous = deepcopy(consumer.artifact_dependencies)
    wheel.write_bytes(b"changed")
    assert client.post(url, json={"artifacts": [str(artifact.id)]}).status_code == 422
    assert consumer.artifact_dependencies == previous


def test_registration_uses_original_source_scheme_not_destination(registry):
    session, root, client = registry
    source = make_project(session, root, kind="factor", scheme="1.2.3")
    consumer = make_project(session, root, scheme="1.2.7")
    source_directory = policy.project_path(source)
    (source_directory / "uv.lock").write_text(tomlkit.dumps({"package": [{"name": "scheme", "version": "1.2.3",
        "source": {"git": "https://original.invalid/scheme#" + "c" * 40}}]}))
    wheel = make_wheel(policy.project_path(consumer) / ".solo-wheels/factor.whl", source.package_name)
    install_id = str(uuid4())
    assert client.post(f"/api/v1/projects/{consumer.id}/dependencies/installations",
                       json={"install_id": install_id}).status_code == 200
    body = {"project_id": str(source.id), "consumer_project_id": str(consumer.id), "install_id": install_id,
            "wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()}
    response = client.post("/api/v1/artifacts/register", json=body)
    assert response.status_code == 201, response.text
    assert response.json()["schemeVersion"] == "1.2.3"
    assert response.json()["sources"]["scheme"]["commit"] == "c" * 40
    (source_directory / "uv.lock").unlink()
    assert client.post("/api/v1/artifacts/register", json=body).status_code == 201


@pytest.mark.parametrize("unused_research", [False, True])
def test_acceptance_uses_exact_installed_subset_not_source_development_graph(registry, unused_research):
    session, root, client = registry
    source, consumer = make_project(session, root, kind="factor"), make_project(session, root)
    if unused_research:
        upstream = make_project(session, root, kind="factor")
        unused = register_file(session, make_wheel(root / "assets/unused.whl", upstream.package_name),
                               upstream, scheme_version="1.2.0")
    else:
        unused = register_asset(session, make_wheel(root / "assets/unused.whl", "unused-helper", "2.0.0", requires_scheme=False))
    source.artifact_dependencies = {unused.package_name: artifact_snapshot(unused)}
    directory = policy.project_path(consumer)
    wheel = make_wheel(directory / ".solo-wheels/factor.whl", source.package_name)
    artifact = register_file(session, wheel, source, scheme_version="1.2.0")
    session.commit()
    (directory / "uv.lock").write_text(tomlkit.dumps({"package": [{"name": "scheme", "version": "1.2.0"},
        {"name": source.package_name, "version": artifact.version, "source": {"path": str(wheel)}}]}))
    response = client.post(f"/api/v1/projects/{consumer.id}/dependencies", json={"artifacts": [str(artifact.id)]})
    assert response.status_code == 200, response.text
    assert response.json() == {"artifacts": [artifact_snapshot(artifact)]}
    assert unused.package_name not in consumer.artifact_dependencies


@pytest.mark.parametrize("unused_research", [False, True])
def test_publication_selection_ignores_unused_retired_upstream_development_dependencies(registry, monkeypatch, unused_research):
    session, root, _ = registry
    if unused_research:
        legacy = make_project(session, root, kind="factor", scheme="1.0.1", template="1.0.1")
        unused = register_file(session, make_wheel(policy.project_path(legacy) / "unused.whl",
            legacy.package_name, "1.0.1", scheme="1.0.1"), legacy, scheme_version="1.0.1", historical=True)
    else:
        unused = register_asset(session, make_wheel(root / "assets/unused.whl", "unused-helper",
            "2.0.0", requires_scheme=False))
    source = make_project(session, root, kind="factor")
    source.artifact_dependencies = {unused.package_name: artifact_snapshot(unused)}
    session.commit()
    selected = register_file(session, make_wheel(policy.project_path(source) / "selected.whl",
        source.package_name), source, scheme_version="1.2.0")
    session.commit()
    consumer = make_project(session, root)
    version, run, _ = make_run(session, root, consumer,
        assets={selected.package_name: artifact_path(selected)})
    accepted = deepcopy(version.accepted_artifacts)
    artifact = publish_version(session, consumer, version)
    session.commit()
    bundle, _ = verify_bundle(artifact)
    before = {path.relative_to(bundle).as_posix(): path.read_bytes()
              for path in bundle.rglob("*") if path.is_file()}
    assert set(artifact.dependencies) == {selected.package_name}
    assert unused.package_name not in policy.local_task_packages(tomllib.loads((bundle / "environment/uv.lock").read_text()))
    policy.check_task(session, version, project=consumer, directory=run, kind=consumer.kind)
    from core.apps.artifacts import service
    required = service.require_artifact
    calls = []

    def exact_only(current, snapshot, **kwargs):
        identifier = (snapshot.get("artifact_id") or snapshot.get("id")) if isinstance(snapshot, dict) else snapshot
        assert str(identifier) != str(unused.id), "An unused upstream development dependency must not be selected"
        calls.append(str(identifier))
        return required(current, snapshot, **kwargs)

    monkeypatch.setattr(service, "require_artifact", exact_only)
    # Missing unused development bytes are just as irrelevant as retired ones.
    artifact_path(unused).unlink()
    chosen = artifact_component(session, artifact.id)
    assert set(chosen["accepted_artifacts"]) == {artifact.package_name, selected.package_name}
    assert set(calls) == {str(artifact.id), str(selected.id)}
    assert version.accepted_artifacts == accepted
    assert chosen["dependencies"][selected.package_name] == accepted[selected.package_name]
    assert {path.relative_to(bundle).as_posix(): path.read_bytes()
            for path in bundle.rglob("*") if path.is_file()} == before


@pytest.mark.parametrize("failure", ["retired", "different_scheme_series"])
def test_publication_selection_rejects_actual_locked_inactive_or_wrong_scheme_dependency(registry, monkeypatch, failure):
    session, root, _ = registry
    source = make_project(session, root, kind="factor", scheme="1.2.3")
    source.template_commit = "c" * 40
    selected = register_file(session, make_wheel(policy.project_path(source) / "selected.whl",
        source.package_name), source, scheme_version="1.2.3")
    session.commit()
    consumer = make_project(session, root)
    version, _, _ = make_run(session, root, consumer,
        assets={selected.package_name: artifact_path(selected)})
    artifact = publish_version(session, consumer, version)
    session.commit()
    assert artifact.sources != selected.sources
    if failure == "retired":
        source_reason = policy.source_reason

        def retire_actual(sources):
            if sources == selected.sources:
                return "Actual locked dependency 已退役"
            return source_reason(sources)

        monkeypatch.setattr(policy, "source_reason", retire_actual)
        with pytest.raises(policy.VersionPolicyError, match="退役"):
            artifact_component(session, artifact.id)
    else:
        # Equivalent to a persisted historical dependency accepted under a
        # different series; change no on-disk/DB immutable fields for the probe.
        from core.apps.artifacts import service
        exact = service.exact_artifacts

        def wrong_scheme(current, identifiers, **kwargs):
            result = exact(current, identifiers, **kwargs)
            for name, snapshot in result.items():
                if name == selected.package_name:
                    snapshot["scheme_version"] = "1.3.0"
            return result

        monkeypatch.setattr(service, "exact_artifacts", wrong_scheme)
        with pytest.raises(ArtifactError, match="exact sealed snapshot"):
            artifact_component(session, artifact.id)


@pytest.mark.parametrize("finish", ["complete", "abort", "expire"])
def test_install_receipt_protects_pending_and_previous_snapshots_until_finalized(registry, finish):
    session, root, client = registry
    source, consumer = make_project(session, root, kind="factor"), make_project(session, root)
    directory = policy.project_path(consumer)
    old_wheel = make_wheel(directory / ".solo-wheels/old.whl", source.package_name, contents="old = 1")
    old = register_file(session, old_wheel, source, scheme_version="1.2.0")
    consumer.artifact_dependencies = {old.package_name: artifact_snapshot(old)}
    session.commit()
    old_lock = tomlkit.dumps({"package": [{"name": "scheme", "version": "1.2.0"},
        {"name": source.package_name, "version": old.version, "source": {"path": str(old_wheel)}}]})
    (directory / "uv.lock").write_text(old_lock)
    url = f"/api/v1/projects/{consumer.id}/dependencies"
    install_id, other_id = str(uuid4()), str(uuid4())
    for nonce in (install_id, other_id):
        response = client.post(url + "/installations", json={"install_id": nonce})
        assert response.status_code == 200, response.text
    wheel = make_wheel(directory / ".solo-wheels/new.whl", source.package_name, contents="new = 2")
    response = client.post("/api/v1/artifacts/register", json={"project_id": str(source.id),
        "consumer_project_id": str(consumer.id), "install_id": install_id, "wheel": str(wheel),
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()})
    assert response.status_code == 201, response.text
    new = session.get(ResearchArtifact, UUID(response.json()["id"]))
    assert consumer.artifact_dependencies == {old.package_name: artifact_snapshot(old)}
    # An unrelated deletion invokes global GC during the install window.
    unrelated = make_project(session, root)
    session.delete(unrelated)
    session.commit()
    assert gc_unreferenced(session) == {"removed": 0, "retained": 2}
    session.commit()
    session.expire_all()
    assert require_artifact(session, new.id)
    assert session.get(ArtifactInstallation, UUID(install_id)) is not None
    if finish == "expire":
        session.get(ArtifactInstallation, UUID(install_id)).expires_at = datetime.now(UTC) - timedelta(seconds=1)
        session.commit()
        assert client.post(url + "/installations", json={"install_id": install_id, "renew": True}).status_code == 409
        assert gc_unreferenced(session)["removed"] == 1
        session.commit()
        assert client.post(url + "/installations", json={"install_id": install_id, "renew": True}).status_code == 409
        assert session.get(ResearchArtifact, new.id) is None
        assert consumer.artifact_dependencies == {old.package_name: artifact_snapshot(old)}
    else:
        (directory / "uv.lock").write_text(tomlkit.dumps({"package": [{"name": "scheme", "version": "1.2.0"},
            {"name": source.package_name, "version": new.version, "source": {"path": str(wheel)}}]}))
        body = {"install_id": install_id, "artifacts": [str(new.id)]}
        accepted = client.post(url, json=body)
        assert accepted.status_code == 200, accepted.text
        assert client.post(url, json=body).json() == accepted.json()
        assert gc_unreferenced(session)["removed"] == 0
        session.commit()
        if finish == "abort":
            # Acceptance committed but its reply could be lost: Jup restores the
            # old files first, and abort restores the durable previous refs.
            (directory / "uv.lock").write_text(old_lock)
        ended = client.post(url + f"/installations/{install_id}/{finish}", json={})
        assert ended.status_code == 200, ended.text
        assert client.post(url + f"/installations/{install_id}/{finish}", json={}).status_code == 200
        expected = old if finish == "abort" else new
        assert consumer.artifact_dependencies == {expected.package_name: artifact_snapshot(expected)}
        assert session.get(ArtifactInstallation, UUID(install_id)) is None
        assert session.get(ArtifactInstallation, UUID(other_id)) is not None
        assert client.post(url + f"/installations/{other_id}/abort", json={}).status_code == 200
        assert gc_unreferenced(session)["removed"] == 1
        session.commit()
        assert require_artifact(session, expected.id)


def test_exact_acceptance_requires_registered_nonresearch_local_assets(registry):
    session, root, client = registry
    consumer = make_project(session, root)
    directory = policy.project_path(consumer)
    url = f"/api/v1/projects/{consumer.id}/dependencies"
    install_id = str(uuid4())
    assert client.post(url + "/installations", json={"install_id": install_id}).status_code == 200
    wheel = make_wheel(directory / ".solo-wheels/helper.whl", "helper", "2.0.0", requires_scheme=False)
    registered = client.post("/api/v1/artifacts/register", json={"project_id": str(consumer.id),
        "consumer_project_id": str(consumer.id), "install_id": install_id, "wheel": str(wheel),
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()})
    assert registered.status_code == 201, registered.text
    assert registered.json()["kind"] == "dependency"
    (directory / "uv.lock").write_text(tomlkit.dumps({"package": [{"name": "scheme", "version": "1.2.0"},
        {"name": "helper", "version": "2.0.0", "source": {"path": str(wheel)}}]}))
    assert client.post(url, json={"install_id": install_id, "artifacts": []}).status_code == 422
    accepted = client.post(url, json={"install_id": install_id, "artifacts": [registered.json()["id"]]})
    assert accepted.status_code == 200, accepted.text
    assert [item["package"] for item in accepted.json()["artifacts"]] == ["helper"]


@pytest.mark.parametrize("tamper", ["input_and_manifest", "empty_manifest"])
def test_database_seal_denies_rewritten_manifest_before_forms_execution(registry, monkeypatch, tamper):
    session, root, client = registry
    project = make_project(session, root)
    version, directory, _ = make_run(session, root, project)
    artifact = publish_version(session, project, version)
    session.commit()
    bundle, _ = verify_bundle(artifact)
    manifest_path = bundle / "manifest.json"
    assert artifact.bundle_manifest_sha256 == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    if tamper == "input_and_manifest":
        input_path = bundle / "input.json"
        data = json.loads(input_path.read_text())
        data["backtest"]["capital"] = 999
        input_path.write_text(json.dumps(data))
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["input.json"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
    else:
        manifest = {"artifact_id": str(artifact.id), "files": {}}
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ArtifactError, match="manifest changed"):
        verify_bundle(artifact)
    from core.apps.strategies import service as strategies
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("tampered bundle must not execute")
    monkeypatch.setattr(strategies, "inspect_component", forbidden)
    response = client.post("/api/v1/strategies/forms", json={"artifacts": {"model": str(artifact.id)}})
    assert response.status_code == 422, response.text
    assert not calls
    artifact.bundle_manifest_sha256 = "0" * 64
    with pytest.raises(ValueError, match="sealed once"):
        session.flush()
    session.rollback()


def test_gc_retains_recursive_live_snapshots_and_strategy_components(registry):
    session, root, _ = registry
    source, consumer = make_project(session, root, kind="factor"), make_project(session, root)
    asset = register_asset(session, make_wheel(root / "assets/helper.whl", "helper", "2.0.0", requires_scheme=False))
    source.artifact_dependencies = {asset.package_name: artifact_snapshot(asset)}
    first = register_file(session, make_wheel(root / "assets/first.whl", source.package_name), source, scheme_version="1.2.0")
    orphan = register_asset(session, make_wheel(root / "assets/orphan.whl", "unused", "1.0.0", requires_scheme=False))
    consumer.artifact_dependencies = {first.package_name: artifact_snapshot(first)}
    session.commit()
    assert gc_unreferenced(session) == {"removed": 1, "retained": 2}
    session.commit()
    assert not artifact_path(orphan).parent.exists()
    consumer.artifact_dependencies = {}
    strategy = Strategy(name="independent", components={"factor": {"artifact_id": str(first.id)}}, forms={})
    session.add(strategy)
    session.delete(source)
    session.commit()
    assert gc_unreferenced(session)["removed"] == 0
    session.delete(strategy)
    session.commit()
    assert gc_unreferenced(session) == {"removed": 2, "retained": 0}


def test_backfill_is_explicit_historical_and_preserves_null_hashes(registry):
    session, root, client = registry
    project = make_project(session, root, scheme="1.0.1", template="1.0.1")
    version, directory, component = make_run(session, root, project, accepted=False, scheme="1.0.1")
    version.input_sha256 = version.lock_sha256 = None
    session.commit()
    originals = (directory / "input.json").read_bytes(), (directory / "environment/uv.lock").read_bytes()
    stats = backfill_tasks(session)
    session.commit()
    assert stats["migrated"] == 1 and stats["failed"] == 0, stats
    assert version.input_sha256 is None and version.lock_sha256 is None
    assert version.accepted_artifacts[project.package_name]["scheme_version"] == "1.0.1"
    assert client.get("/api/v1/artifacts").json()[0]["retired"] is True
    assert ((directory / "input.json").read_bytes(), (directory / "environment/uv.lock").read_bytes()) == originals
    assert backfill_tasks(session)["skipped"] == 1
    with pytest.raises(ValueError):
        publish_version(session, project, version)


def test_backfill_missing_source_fails_closed_and_keeps_existing_snapshots(registry):
    session, root, _ = registry
    project = make_project(session, root)
    complete, _, _ = make_run(session, root, project)
    snapshots = deepcopy(complete.accepted_artifacts)
    broken, directory, component = make_run(session, root, project, accepted=False)
    Path(component["wheel"]).unlink()
    stats = backfill_tasks(session)
    assert stats["failed"] == 1 and stats["skipped"] == 1
    assert complete.accepted_artifacts == snapshots
    assert not broken.accepted_artifacts


def test_real_uv_bundle_sync_locked_after_source_run_deletion(registry):
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is unavailable")
    session, root, _ = registry
    project = make_project(session, root)
    helper = make_wheel(root / "assets/helper-2.0.1-py3-none-any.whl", "helper", "2.0.1", requires_scheme=False)
    version, directory, component = make_run(session, root, project, assets={"helper": helper}, flat=True, accepted=False)
    scheme = make_wheel(directory / "wheels/scheme-1.2.0-py3-none-any.whl", "scheme", "1.2.0", requires_scheme=False)
    environment = directory / "environment"
    config = tomllib.loads((environment / "pyproject.toml").read_text())
    config["tool"]["uv"]["sources"]["scheme"] = {"path": str(scheme)}
    config["project"]["dependencies"].extend(["helper==2.0.1", "scheme==1.2.0"])
    config["tool"]["uv"]["constraint-dependencies"] = ["helper==2.0.1", "scheme==1.2.0"]
    config["tool"]["uv"]["override-dependencies"] = ["helper==2.0.1"]
    config["tool"]["uv"]["index"] = [item for item in config["tool"]["uv"]["index"] if item.get("format") == "flat"]
    (environment / "pyproject.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    (environment / "uv.lock").unlink()
    result = subprocess.run([uv, "lock", "--offline", "--python", sys.executable, "--project", str(environment)],
                            text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stderr
    version.lock_sha256 = hashlib.sha256((environment / "uv.lock").read_bytes()).hexdigest()
    version.accepted_artifacts = policy.capture_task_artifacts(session, version, project=project, kind=project.kind)
    (directory / "report/run.json").write_text(json.dumps({"status": "success", "input_sha256": version.input_sha256,
        "lock_sha256": version.lock_sha256, "versions": {"scheme": "1.2.0"}}))
    session.commit()
    artifact = publish_version(session, project, version)
    session.commit()
    bundle, data = verify_bundle(artifact)
    expected = (bundle / "environment/uv.lock").read_bytes()
    shutil.rmtree(directory)
    for arguments in (["lock", "--check"], ["sync", "--locked", "--no-dev"]):
        result = subprocess.run([uv, *arguments, "--offline", "--python", sys.executable,
                                 "--project", str(bundle / "environment")], text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stderr
    assert (bundle / "environment/uv.lock").read_bytes() == expected
    verify_bundle(artifact)


def test_migration_preserves_legacy_identity_and_null_task_snapshots(tmp_path):
    engine = create_engine("sqlite://")
    migration_root = Path(__file__).resolve().parents[1] / "alembic/versions"
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            for file in sorted(migration_root.glob("*.py")):
                spec = importlib.util.spec_from_file_location(file.stem, file)
                migration = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(migration)
                if file.name.startswith("0007"):
                    project_id, version_id = uuid4().hex, uuid4().hex
                    connection.exec_driver_sql("INSERT INTO projects (id,name,description,kind,template_tag,template_commit,archived,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (project_id, "legacy", "", "factor", "v1.0.1", "a" * 40, 1, "2026-01-01", "2026-01-01"))
                    connection.exec_driver_sql("INSERT INTO project_versions (id,project_id,number,package_version,note,status,parameters,error,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                        (version_id, project_id, 1, "1.0.1", "", "success", "{}", "", "2026-01-01"))
                migration.upgrade()
        row = connection.exec_driver_sql("SELECT package_name, artifact_dependencies FROM projects").one()
        assert row[0] == "factor-" + project_id[:4] and json.loads(row[1]) == {}
        assert connection.exec_driver_sql("SELECT input_sha256,lock_sha256,accepted_artifacts FROM project_versions").one() == (None, None, None)
        assert inspect(connection).get_foreign_keys("project_versions")[0]["options"]["ondelete"] == "CASCADE"
        assert inspect(connection).get_foreign_keys("research_artifacts") == []
    engine.dispose()
