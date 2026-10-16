"""UUID artifacts and publication use only temporary files and SQLite."""

import importlib.util
import json
import shutil
import tomllib
from pathlib import Path
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
import tomlkit
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.artifacts import service
from core.apps.artifacts.models import ResearchArtifact
from core.apps.artifacts.service import (ArtifactError, artifact_component, artifact_path, artifact_snapshot,
                                        checked_path, gc_unreferenced, publish_version, register_file, verify_bundle, wheel_metadata)
from core.apps.artifacts.views import router as artifact_router
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies.models import Strategy
from core.apps.version_policy_views import router as policy_router
from core.database.base import Base
from core.database.session import get_database_session


@pytest.fixture
def registry(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    session = Session(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(artifact_router)
    app.include_router(policy_router)
    app.dependency_overrides[get_database_session] = lambda: session
    try:
        with TestClient(app) as client:
            yield session, tmp_path, client
    finally:
        session.close()
        engine.dispose()


def make_project(session, root, *, kind="model", name=None, scheme="1.2.0", template="1.2.0"):
    project = Project(id=uuid4(), name=name or str(uuid4()), kind=kind,
        template_tag="v" + template, template_commit="a" * 40, template_package_version=template,
        scheme_version="v" + scheme, scheme_commit="b" * 40)
    session.add(project)
    session.commit()
    directory = root / "projects" / kind / project.name
    directory.mkdir(parents=True)
    (directory / ".solo").write_text(json.dumps({"project_id": str(project.id), "name": project.name, "kind": kind}))
    (directory / "uv.lock").write_text(tomlkit.dumps({"package": [{"name": "scheme", "version": scheme}]}))
    return project


def make_wheel(path, package, version="1.2.1", *, scheme="1.2.0", requires_scheme=True, entry=None, module=True, dependencies=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    name = package.replace("-", "_")
    series = ".".join(scheme.split(".")[:2])
    requirement = f"Requires-Dist: scheme>={series}.0,<{scheme.split('.')[0]}.{int(scheme.split('.')[1]) + 1}.0\n" if requires_scheme else ""
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{name}-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n{requirement}"
            + "".join(f"Requires-Dist: {value}\n" for value in dependencies)
            + (f"Entry: {entry}\n" if entry else ""))
        archive.writestr(f"{name}-{version}.dist-info/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{name}-{version}.dist-info/RECORD", "")
        if module:
            archive.writestr(f"{name}/__init__.py", "")
    return path


def make_run(session, root, project, *, assets=None, flat=False, accepted=True, scheme="1.2.0"):
    number = (session.scalar(select(ProjectVersion.number).where(ProjectVersion.project_id == project.id)
              .order_by(ProjectVersion.number.desc())) or 0) + 1
    version = ProjectVersion(id=uuid4(), project_id=project.id, number=number,
        package_version=f"{'.'.join(scheme.split('.')[:2])}.{number}", status="success", scheme_version=scheme)
    session.add(version)
    session.commit()
    directory = root / "runs" / str(version.id)
    environment = directory / "environment"
    environment.mkdir(parents=True)
    wheel = make_wheel(directory / "wheels" / f"{project.package_name.replace('-', '_')}-{version.package_version}-py3-none-any.whl",
                       project.package_name, version.package_version, scheme=scheme)
    component = {"package": project.package_name, "version": version.package_version,
        "entry": f"{project.package_name.replace('-', '_')}:{'Factor' if project.kind == 'factor' else project.kind.title() + 'Algo'}",
        "wheel": str(wheel)}
    packages = [{"name": "scheme", "version": scheme, "source": {"git": "https://example.invalid/scheme#" + "b" * 40}},
                {"name": project.package_name, "version": version.package_version, "source": {"path": str(wheel)},
                 "wheels": [{"filename": wheel.name}]}]
    sources = {project.package_name: {"path": str(wheel)}, "scheme": {"git": "https://example.invalid/scheme", "rev": "b" * 40}}
    indexes = [{"name": "private", "url": "https://private.example.invalid/simple", "default": True}]
    from core.apps.artifacts.service import wheel_metadata
    for name, source in (assets or {}).items():
        copied = directory / "wheels" / name / source.name
        copied.parent.mkdir(parents=True)
        shutil.copyfile(source, copied)
        _, value, _, _ = wheel_metadata(copied)
        if flat:
            packages.append({"name": name, "version": value, "source": {"registry": copied.parent.as_posix()},
                             "wheels": [{"path": copied.name}]})
            sources[name] = {"index": "local-" + name}
            indexes.append({"name": "local-" + name, "url": str(copied.parent), "format": "flat"})
        else:
            packages.append({"name": name, "version": value, "source": {"path": str(copied)},
                             "wheels": [{"filename": copied.name}]})
            sources[name] = {"path": str(copied)}
    (environment / "uv.lock").write_text(tomlkit.dumps({"version": 1, "package": packages}), encoding="utf-8")
    (environment / "pyproject.toml").write_text(tomlkit.dumps({"project": {"name": "research-environment",
        "version": "0.0.0", "requires-python": ">=3.12", "dependencies": [project.package_name + "==" + version.package_version]},
        "tool": {"uv": {"package": False, "sources": sources, "index": indexes}}}), encoding="utf-8")
    data = {"kind": project.kind, "environment": {"lockfile": str(environment / "uv.lock")}, "output": str(directory / "report")}
    if project.kind == "factor":
        data.update(factor={**component, "params": {}}, analysis={})
    else:
        data.update(algos={project.kind: component}, backtest={"capital": 1000})
    (directory / "input.json").write_text(json.dumps(data), encoding="utf-8")
    (directory / "report").mkdir()
    (directory / "report/run.json").write_text(json.dumps({"protocol": 1, "status": "success", "reports": {}, "versions": {"scheme": scheme}}))
    if accepted:
        version.accepted_artifacts = policy.capture_task_artifacts(session, version, project=project, kind=project.kind)
    session.commit()
    return version, directory, component


def test_register_always_creates_uuid_and_downloads_original_file(registry):
    session, root, client = registry
    producer = make_project(session, root, kind="factor")
    consumer = make_project(session, root)
    entry = producer.package_name.replace("-", "_") + ".native:Factor"
    wheel = make_wheel(policy.project_path(consumer) / ".solo-wheels/candidate.whl", producer.package_name,
                       entry=entry, module=False)
    results = [client.post("/api/v1/artifacts/register", json={"project_id": str(producer.id), "wheel": str(wheel)}) for _ in range(2)]
    assert all(response.status_code == 201 for response in results)
    first, second = [response.json() for response in results]
    assert first["id"] != second["id"] and first["entry"] == entry
    artifact = session.get(ResearchArtifact, UUID(first["id"]))
    assert artifact.storage_directory == str(artifact.id)
    assert first["sourceProjectId"] == str(producer.id)
    response = client.get(f"/api/v1/artifacts/{artifact.id}/wheel")
    assert response.status_code == 200
    assert f'filename="{wheel.name}"' in response.headers["content-disposition"]
    assert wheel_metadata(artifact_path(artifact))[:2] == (producer.package_name, "1.2.1")
    assert client.get(f"/api/v1/artifacts/{artifact.id}").json() == first
    assert len(client.get("/api/v1/artifacts").json()) == 2
    assert "sha256" not in first and "bundleManifestSha256" not in first
    assert "sha256" not in artifact_snapshot(artifact)


def test_artifact_metadata_exposes_exact_legacy_storage_mapping_for_accepted_uuid(registry):
    session, root, client = registry
    project = make_project(session, root)
    wheel = make_wheel(root / "local/helper.whl", "custom-helper", "7.0.0", requires_scheme=False)
    selected, other = [register_file(session, wheel) for _ in range(2)]
    original = artifact_path(selected).parent
    selected.storage_directory = "a1" * 32  # Artificial legacy directory, not a content digest.
    original.rename(artifact_path(selected).parent)
    session.commit()
    accepted = client.post(f"/api/v1/projects/{project.id}/dependencies", json={"artifacts": [str(selected.id)]})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["artifacts"][0]["id"] == str(selected.id)
    records = client.get("/api/v1/artifacts").json()
    assert {item["id"] for item in records} == {str(selected.id), str(other.id)}
    response = client.get(f"/api/v1/artifacts/{selected.id}")
    assert response.status_code == 200
    assert response.json()["storageDirectory"] == "a1" * 32
    assert response.json()["id"] == str(selected.id)
    assert "sha256" not in response.json()
    assert client.get(f"/api/v1/artifacts/{other.id}").json()["storageDirectory"] == str(other.id)
    assert client.get(f"/api/v1/artifacts/{selected.id}/wheel").status_code == 200
    assert project.artifact_dependencies["custom-helper"]["id"] == str(selected.id)
    assert project.artifact_dependencies["custom-helper"]["id"] != str(other.id)


@pytest.mark.parametrize("stage", ["flush", "commit"])
def test_registration_transaction_failure_removes_only_new_directory_and_allows_retry(registry, monkeypatch, stage):
    session, root, client = registry
    project = make_project(session, root)
    wheel = make_wheel(root / "local/helper.whl", "custom-helper", "7.0.0", requires_scheme=False)
    existing = register_file(session, wheel)
    session.commit()
    original = getattr(session, stage)
    failure = SQLAlchemyError("registration rejected")

    def reject(*args, **kwargs):
        if stage == "commit" or any(isinstance(item, ResearchArtifact) for item in session.new):
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(session, stage, reject)
    response = client.post("/api/v1/artifacts/register", json={"project_id": str(project.id), "wheel": str(wheel)})
    assert response.status_code == 503, response.text
    assert list((root / "artifacts").iterdir()) == [artifact_path(existing).parent]
    assert list(session.scalars(select(ResearchArtifact))) == [existing]
    assert wheel.is_file() and artifact_path(existing).is_file()
    monkeypatch.setattr(session, stage, original)
    response = client.post("/api/v1/artifacts/register", json={"project_id": str(project.id), "wheel": str(wheel)})
    assert response.status_code == 201, response.text
    registered = session.get(ResearchArtifact, UUID(response.json()["id"]))
    assert registered.id != existing.id and artifact_path(registered).is_file()


def test_registration_never_cleans_existing_uuid_directory(registry, monkeypatch):
    session, root, _ = registry
    wheel = make_wheel(root / "local/helper.whl", "helper", requires_scheme=False)
    existing = register_file(session, wheel)
    session.commit()
    monkeypatch.setattr(service, "uuid4", lambda: existing.id)
    with pytest.raises(FileExistsError):
        register_file(session, wheel)
    assert artifact_path(existing).is_file()
    assert list((root / "artifacts").iterdir()) == [artifact_path(existing).parent]


def test_register_plain_dependency_with_current_project(registry):
    session, root, client = registry
    project = make_project(session, root)
    wheel = make_wheel(root / "local/helper.whl", "custom-helper", "7.0.0", requires_scheme=False)
    response = client.post("/api/v1/artifacts/register", json={"project_id": str(project.id), "wheel": str(wheel)})
    assert response.status_code == 201, response.text
    assert response.json()["kind"] == "dependency"
    accepted = client.post(f"/api/v1/projects/{project.id}/dependencies", json={"artifacts": [response.json()["id"]]})
    assert accepted.status_code == 200, accepted.text
    assert list(project.artifact_dependencies) == ["custom-helper"]
    assert client.post(f"/api/v1/projects/{project.id}/dependencies", json={"artifacts": []}).json() == {"artifacts": []}
    assert client.post(f"/api/v1/projects/{project.id}/dependencies/installations", json={}).status_code == 404


def test_publish_source_independent_and_no_manifest_digests(registry, monkeypatch):
    session, root, client = registry
    project = make_project(session, root)
    version, directory, component = make_run(session, root, project)

    def copy(*args, **kwargs):
        raise AssertionError("The registered root and original inputs must not be copied on publication")

    monkeypatch.setattr(service.shutil, "copyfile", copy)
    response = client.post(f"/api/v1/projects/{project.id}/versions/{version.id}/publish", json={})
    assert response.status_code == 200, response.text
    artifact = session.get(ResearchArtifact, UUID(response.json()["artifactId"]))
    bundle = Path(artifact.bundle_path)
    assert {path.name for path in bundle.iterdir()} == {"environment", "input.json", "manifest.json"}
    manifest = json.loads((bundle / "manifest.json").read_text())
    assert set(manifest) == {"artifact_id", "root_artifact", "accepted_artifacts"}
    assert "sha256" not in json.dumps(manifest)
    bundled_component = json.loads((bundle / "input.json").read_text())["algos"]["model"]
    assert set(bundled_component) == {"package", "version", "wheel", "entry"}
    assert Path(bundled_component["wheel"]) == artifact_path(artifact)
    assert client.post(f"/api/v1/projects/{project.id}/versions/{version.id}/publish", json={}).status_code == 200
    assert Path(component["wheel"]).is_file()
    session.delete(version)
    session.delete(project)
    session.commit()
    shutil.rmtree(directory)
    assert verify_bundle(artifact)[0] == bundle.resolve()
    assert artifact_component(session, artifact.id)["component"]["package"] == artifact.package_name
    assert client.get(f"/api/v1/artifacts/{artifact.id}/wheel").status_code == 200
    assert wheel_metadata(artifact_path(artifact))[:2] == (artifact.package_name, artifact.version)


@pytest.mark.parametrize("flat", [False, True])
def test_publish_reuses_registered_wheels_and_copies_only_unregistered_local_assets(registry, monkeypatch, flat):
    session, root, client = registry
    producer = make_project(session, root, kind="factor")
    dependency_version, dependency_directory, _ = make_run(session, root, producer)
    dependency = publish_version(session, producer, dependency_version)
    session.commit()
    project = make_project(session, root)
    project.artifact_dependencies = {dependency.package_name: artifact_snapshot(dependency)}
    version, directory, _ = make_run(session, root, project,
        assets={dependency.package_name: artifact_path(dependency)}, flat=flat)
    root_artifact = session.get(ResearchArtifact, UUID(version.accepted_artifacts[project.package_name]["id"]))
    helper = make_wheel(directory / "wheels/custom-helper/custom_helper-7.0.0-py3-none-any.whl",
                        "custom-helper", "7.0.0", requires_scheme=False)
    lockfile = directory / "environment/uv.lock"
    lock = tomllib.loads(lockfile.read_text())
    lock["package"].append({"name": "custom-helper", "version": "7.0.0",
        "source": {"registry": helper.parent.as_posix()} if flat else {"path": str(helper)},
        "wheels": [{"path": helper.name}]})
    requirement = {"name": dependency.package_name, "index": str(directory / "wheels" / dependency.package_name)} if flat else {
        "name": dependency.package_name, "path": str(directory / "wheels" / dependency.package_name / dependency.filename)}
    lock["manifest"] = {"requirements": [requirement.copy()]}
    lock["package"][1]["metadata"] = {"requires-dist": [requirement.copy()]}
    lockfile.write_text(tomlkit.dumps(lock))
    configfile = lockfile.with_name("pyproject.toml")
    config = tomllib.loads(configfile.read_text())
    config["tool"]["uv"]["sources"]["custom-helper"] = {"index": "custom-helper"} if flat else {"path": str(helper)}
    if flat:
        config["tool"]["uv"]["index"].append({"name": "custom-helper", "url": str(helper.parent), "format": "flat"})
    configfile.write_text(tomlkit.dumps(config))
    copies, asset_reads = [], []
    copyfile, local_assets = service.shutil.copyfile, service._local_assets

    def copy(source, target, *args, **kwargs):
        copies.append((Path(source), Path(target)))
        return copyfile(source, target, *args, **kwargs)

    def assets(*args):
        asset_reads.append(args)
        return local_assets(*args)

    monkeypatch.setattr(service.shutil, "copyfile", copy)
    monkeypatch.setattr(service, "_local_assets", assets)
    artifact = publish_version(session, project, version)
    session.commit()
    assert artifact.id == root_artifact.id
    assert len(asset_reads) == 1
    bundle, data = verify_bundle(artifact)
    unregistered = bundle / "wheels/custom-helper" / helper.name
    helper_artifact = session.get(ResearchArtifact, UUID(artifact.dependencies["custom-helper"]["id"]))
    assert copies == [(helper.resolve(), artifact_path(helper_artifact)), (helper.resolve(), unregistered)]
    assert list((bundle / "wheels").rglob("*.whl")) == [unregistered]
    assert not (bundle / "original").exists()
    expected = {project.package_name: artifact_path(artifact), dependency.package_name: artifact_path(dependency),
                "custom-helper": unregistered}
    bundled_lock = tomllib.loads((bundle / "environment/uv.lock").read_text())
    bundled_sources = tomllib.loads((bundle / "environment/pyproject.toml").read_text())["tool"]["uv"]["sources"]
    for name, target in expected.items():
        package = next(item for item in bundled_lock["package"] if item["name"] == name)
        assert package["source"] == {"path": str(target)}
        assert package["wheels"] == [{"filename": target.name}]
        assert bundled_sources[name] == {"path": str(target)}
    rebased_requirement = {"name": dependency.package_name, "path": artifact_path(dependency).as_posix()}
    assert bundled_lock["manifest"]["requirements"] == [rebased_requirement]
    assert bundled_lock["package"][1]["metadata"]["requires-dist"] == [rebased_requirement]
    assert Path(data["algos"]["model"]["wheel"]) == artifact_path(artifact)
    assert artifact.dependencies[dependency.package_name]["id"] == str(dependency.id)
    downloads = {str(item.id): (item.package_name, item.version, artifact_path(item))
                 for item in (artifact, dependency, helper_artifact)}
    session.delete(version)
    session.delete(dependency_version)
    session.delete(project)
    session.delete(producer)
    session.commit()
    shutil.rmtree(directory)
    shutil.rmtree(dependency_directory)
    for identifier, (package, version, wheel) in downloads.items():
        assert client.get(f"/api/v1/artifacts/{identifier}").status_code == 200
        assert client.get(f"/api/v1/artifacts/{identifier}/wheel").status_code == 200
        assert wheel_metadata(wheel)[:2] == (package, version)
    assert wheel_metadata(unregistered)[:2] == ("custom-helper", "7.0.0")
    assert artifact_component(session, artifact.id)["environment_path"] == str(bundle / "environment")
    response = client.delete(f"/api/v1/artifacts/{dependency.id}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "artifact_in_use"


@pytest.mark.parametrize("stage", ["dependency-flush", "publication-flush", "commit"])
def test_publication_transaction_failure_cleans_bundle_and_new_artifacts_and_allows_retry(registry, monkeypatch, stage):
    session, root, client = registry
    project = make_project(session, root)
    version, directory, _ = make_run(session, root, project)
    artifact = session.get(ResearchArtifact, UUID(version.accepted_artifacts[project.package_name]["id"]))
    version.finished_at = version.created_at
    session.commit()
    helper = make_wheel(directory / "wheels/helper-7.0.0-py3-none-any.whl", "helper", "7.0.0", requires_scheme=False)
    lockfile = directory / "environment/uv.lock"
    lock = tomllib.loads(lockfile.read_text())
    lock["package"].append({"name": "helper", "version": "7.0.0", "source": {"path": str(helper)},
                            "wheels": [{"filename": helper.name}]})
    lockfile.write_text(tomlkit.dumps(lock))
    bundle = artifact_path(artifact).parent / "bundle"
    operation = "commit" if stage == "commit" else "flush"
    original = getattr(session, operation)
    failure = SQLAlchemyError(stage)

    def reject(*args, **kwargs):
        if stage == "commit" or (stage == "publication-flush" and bundle.exists()) or (
                stage == "dependency-flush" and any(isinstance(item, ResearchArtifact) for item in session.new)):
            raise failure
        return original(*args, **kwargs)

    monkeypatch.setattr(session, operation, reject)
    url = f"/api/v1/projects/{project.id}/versions/{version.id}/publish"
    response = client.post(url, json={})
    assert response.status_code == 503, response.text
    assert not bundle.exists() and artifact_path(artifact).is_file()
    assert list((root / "artifacts").iterdir()) == [artifact_path(artifact).parent]
    assert list(session.scalars(select(ResearchArtifact))) == [artifact]
    assert artifact.published_at is None and artifact.bundle_path is None
    monkeypatch.setattr(session, operation, original)
    response = client.post(url, json={})
    assert response.status_code == 200, response.text
    dependency = session.get(ResearchArtifact, UUID(artifact.dependencies["helper"]["id"]))
    assert artifact_path(dependency).is_file()
    assert bundle.is_dir() and Path(artifact.bundle_path) == bundle


def test_publication_cleanup_reraises_original_transaction_error(registry, monkeypatch):
    session, root, _ = registry
    project = make_project(session, root)
    version, _, _ = make_run(session, root, project)
    artifact = session.get(ResearchArtifact, UUID(version.accepted_artifacts[project.package_name]["id"]))
    failure = SQLAlchemyError("publication rejected")

    def reject():
        raise failure

    monkeypatch.setattr(session, "commit", reject)
    with pytest.raises(SQLAlchemyError) as caught:
        service.publish_version_committed(session, project, version)
    assert caught.value is failure
    session.rollback()
    assert not (artifact_path(artifact).parent / "bundle").exists()


@pytest.mark.parametrize("published", [False, True])
def test_publication_failure_never_removes_an_existing_bundle(registry, monkeypatch, published):
    session, root, client = registry
    project = make_project(session, root)
    version, _, _ = make_run(session, root, project)
    artifact = session.get(ResearchArtifact, UUID(version.accepted_artifacts[project.package_name]["id"]))
    bundle = artifact_path(artifact).parent / "bundle"
    if published:
        publish_version(session, project, version)
        session.commit()
    else:
        bundle.mkdir()
        (bundle / "owned-marker").touch()

    def reject():
        raise SQLAlchemyError("commit rejected")

    monkeypatch.setattr(session, "commit", reject)
    response = client.post(f"/api/v1/projects/{project.id}/versions/{version.id}/publish", json={})
    assert response.status_code == (503 if published else 409), response.text
    assert bundle.is_dir()
    assert (bundle / ("manifest.json" if published else "owned-marker")).is_file()
    assert artifact_path(artifact).is_file()


def test_publish_keeps_unused_path_text_and_ignores_unselected_local_source(registry):
    session, root, _ = registry
    project = make_project(session, root)
    version, directory, _ = make_run(session, root, project)
    configfile = directory / "environment/pyproject.toml"
    config = tomllib.loads(configfile.read_text())
    config["tool"]["uv"]["sources"]["unused"] = [{"path": str(root / "projects/missing.whl")}]
    config["project"]["description"] = str(root / "projects/description-only")
    configfile.write_text(tomlkit.dumps(config))
    artifact = publish_version(session, project, version)
    bundled = tomllib.loads((Path(artifact.bundle_path) / "environment/pyproject.toml").read_text())
    assert "unused" not in bundled["tool"]["uv"]["sources"]
    assert bundled["project"]["description"] == config["project"]["description"]


def test_published_package_version_is_unique_but_registration_still_creates_new_uuid(registry):
    session, root, client = registry
    project = make_project(session, root)
    version, _, component = make_run(session, root, project)
    published = publish_version(session, project, version)
    session.commit()
    response = client.post("/api/v1/artifacts/register", json={"project_id": str(project.id), "wheel": component["wheel"]})
    assert response.status_code == 201, response.text
    replacement = session.get(ResearchArtifact, UUID(response.json()["id"]))
    assert replacement.id != published.id and replacement.version == published.version
    version.accepted_artifacts = {replacement.package_name: artifact_snapshot(replacement)}
    session.commit()
    response = client.post(f"/api/v1/projects/{project.id}/versions/{version.id}/publish", json={})
    assert response.status_code == 409 and response.json()["detail"]["code"] == "artifact_conflict"
    assert not (artifact_path(replacement).parent / "bundle").exists()
    assert artifact_path(published).is_file()


def test_gc_retains_accepted_and_removes_only_unreferenced_uuid(registry):
    session, root, _ = registry
    project = make_project(session, root)
    kept = register_file(session, make_wheel(root / "kept.whl", "kept", requires_scheme=False))
    removed = register_file(session, make_wheel(root / "removed.whl", "removed", requires_scheme=False))
    project.artifact_dependencies = {kept.package_name: artifact_snapshot(kept)}
    session.commit()
    assert gc_unreferenced(session) == {"removed": 1, "retained": 1}
    assert artifact_path(kept).exists() and not artifact_path(removed).exists()


@pytest.mark.parametrize("reference", ["published", "project", "version", "strategy"])
@pytest.mark.parametrize("key", ["id", "artifact_id"])
def test_gc_uses_real_roots_and_reclaims_unreferenced_chains_and_cycles_once(registry, reference, key):
    session, root, _ = registry
    artifacts = {name: register_file(session, make_wheel(root / f"{name}.whl", name, requires_scheme=False))
                 for name in ("root", "id", "artifact-id", "chain-a", "chain-b", "cycle-a", "cycle-b")}
    def snapshot(name):
        return {key: str(artifacts[name].id)}
    retained = [artifacts[name] for name in ("root", "id", "artifact-id")]
    removed = [artifacts[name] for name in ("chain-a", "chain-b", "cycle-a", "cycle-b")]
    artifacts["root"].dependencies = {"id": snapshot("id")}
    artifacts["id"].dependencies = {"artifact_id": snapshot("artifact-id")}
    artifacts["artifact-id"].dependencies = {"root": snapshot("root")}
    artifacts["chain-a"].dependencies = {"chain-b": snapshot("chain-b")}
    artifacts["cycle-a"].dependencies = {"cycle-b": snapshot("cycle-b")}
    artifacts["cycle-b"].dependencies = {"cycle-a": snapshot("cycle-a")}
    selected = {"id": snapshot("root")}
    if reference == "published":
        from core.apps.projects.models import utc_now
        artifacts["root"].published_at = utc_now()
    elif reference == "project":
        project = make_project(session, root)
        project.artifact_dependencies = selected
    elif reference == "version":
        project = make_project(session, root)
        session.add(ProjectVersion(project_id=project.id, number=1, package_version="1.2.1",
                                   accepted_artifacts=selected))
    else:
        session.add(Strategy(name="retained", forms={}, components={}, accepted_artifacts=selected))
    session.commit()
    assert gc_unreferenced(session) == {"removed": 4, "retained": 3}
    session.commit()
    assert {item.id for item in session.scalars(select(ResearchArtifact))} == {item.id for item in retained}
    assert all(artifact_path(item).is_file() for item in retained)
    assert all(not artifact_path(item).parent.exists() for item in removed)


@pytest.mark.parametrize("value", [{"id": 123}, {"artifact_id": ["not-a-snapshot"]},
                                  {"id": {"artifact_id": "accepted"}}, [{"id": "accepted"}]])
def test_gc_snapshot_ids_are_strings_and_package_maps_remain_traversable(value):
    expected = ["accepted"] if isinstance(value, list) or isinstance(value.get("id"), dict) else []
    assert list(service._snapshot_ids(value)) == expected


def test_checked_path_allows_internal_links_but_rejects_resolved_escape(registry, tmp_path):
    _, root, _ = registry
    owned = root / "owned"
    owned.mkdir()
    file = owned / "file.whl"
    file.write_bytes(b"wheel")
    link = owned / "link.whl"
    try:
        link.symlink_to(file)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    assert checked_path(link, owned) == file.resolve()
    with pytest.raises(ArtifactError):
        checked_path(file, owned / "other")


def test_migration_0009_preserves_old_directory_strings_without_file_access(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'migration.sqlite3').as_posix()}")
    modules = []
    folder = Path(__file__).resolve().parents[1] / "alembic/versions"
    for filename in ("0007_artifact_registry.py", "0008_artifact_installations_and_bundle_seal.py", "0009_uuid_artifacts.py"):
        spec = importlib.util.spec_from_file_location(filename[:-3], folder / filename)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        modules.append(module)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE projects (id CHAR(32) PRIMARY KEY, kind TEXT)"))
        connection.execute(text("CREATE TABLE project_versions (id CHAR(32) PRIMARY KEY, project_id CHAR(32) REFERENCES projects(id), input_sha256 TEXT, lock_sha256 TEXT)"))
        connection.execute(text("CREATE TABLE strategies (id CHAR(32) PRIMARY KEY, input_sha256 TEXT, lock_sha256 TEXT)"))
        with Operations.context(MigrationContext.configure(connection, opts={"version_table": "alembic_version_backend"})):
            modules[0].upgrade()
            modules[1].upgrade()
            for index in range(5):
                connection.execute(text("INSERT INTO research_artifacts (id,kind,package_name,version,sha256,filename,size_bytes,entry,sources,dependencies,created_at) "
                    "VALUES (:id,'model',:package,'1.2.1',:directory,'candidate.whl',1,'model:Algo','{}','{}','2026-10-15')"),
                    {"id": uuid4().hex, "package": f"model-{index:04d}", "directory": f"{index:064x}"})
            modules[2].upgrade()
            assert connection.execute(text("SELECT storage_directory FROM research_artifacts ORDER BY package_name")).scalars().all() == [f"{index:064x}" for index in range(5)]
            columns = {column["name"] for column in inspect(connection).get_columns("research_artifacts")}
            assert not {"sha256", "bundle_manifest_sha256"} & columns
            assert "artifact_installations" not in inspect(connection).get_table_names()
            with pytest.raises(RuntimeError, match="cannot be downgraded.*storage_directory"):
                modules[2].downgrade()
            assert connection.execute(text("SELECT storage_directory FROM research_artifacts ORDER BY package_name")).scalars().all() == [f"{index:064x}" for index in range(5)]
            assert {column["name"] for column in inspect(connection).get_columns("research_artifacts")} == columns
            assert "artifact_installations" not in inspect(connection).get_table_names()
    engine.dispose()
