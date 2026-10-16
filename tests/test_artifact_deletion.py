"""Deletion preserves user data and live UUID references."""

import json
import shutil
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from core.apps.artifacts import deletion
from core.apps.artifacts.models import ResearchArtifact
from core.apps.artifacts.service import artifact_path, artifact_snapshot, publish_version, register_file, wheel_metadata
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies.models import Strategy
from test_artifacts import make_project, make_run, make_wheel
from test_artifacts import registry as registry


def published(session, root):
    project = make_project(session, root)
    version, run, component = make_run(session, root, project)
    artifact = publish_version(session, project, version)
    session.commit()
    return project, version, run, component, artifact


def test_delete_only_selected_artifact_and_preserves_source_run(registry):
    session, root, client = registry
    project, version, run, component, artifact = published(session, root)
    identifier = artifact.id
    wheel = Path(component["wheel"])
    expected = (project.package_name, version.package_version)
    response = client.delete(f"/api/v1/artifacts/{identifier}")
    assert response.status_code == 204, response.text
    with Session(session.bind) as current:
        assert current.get(ResearchArtifact, identifier) is None
        assert current.get(Project, project.id) is not None
        assert current.get(ProjectVersion, version.id) is not None
    assert not artifact_path(artifact).parent.exists()
    assert wheel_metadata(wheel)[:2] == expected and (run / "report/run.json").is_file()
    assert client.get(f"/api/v1/artifacts/{identifier}").status_code == 404
    assert client.get(f"/api/v1/artifacts/{identifier}/wheel").status_code == 404


@pytest.mark.parametrize("reference", ["project", "strategy", "artifact"])
def test_current_uuid_references_block_deletion(registry, reference):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    snapshot = artifact_snapshot(artifact)
    if reference == "project":
        consumer = make_project(session, root)
        consumer.artifact_dependencies = {artifact.package_name: snapshot}
    elif reference == "strategy":
        session.add(Strategy(name="retained strategy", forms={}, status="success",
            components={"model": {"artifact_id": str(artifact.id)}}, accepted_artifacts={}))
    else:
        other = register_file(session, make_wheel(root / "other.whl", "helper", requires_scheme=False))
        other.dependencies = {artifact.package_name: snapshot}
    session.commit()
    response = client.delete(f"/api/v1/artifacts/{artifact.id}")
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "artifact_in_use"
    assert response.json()["detail"]["refs"][0]["type"] == reference
    assert artifact_path(artifact).is_file()


def test_failed_commit_restores_original_owned_directory(registry, monkeypatch):
    session, root, _ = registry
    _, _, _, _, artifact = published(session, root)
    expected = (artifact.package_name, artifact.version)
    def failed():
        raise RuntimeError("commit rejected")
    monkeypatch.setattr(session, "commit", failed)
    with pytest.raises(RuntimeError, match="commit rejected"):
        deletion.delete_artifact(artifact.id, session)
    assert session.get(ResearchArtifact, artifact.id) is not None
    assert wheel_metadata(artifact_path(artifact))[:2] == expected
    assert not (root / "artifacts" / f".delete-{artifact.id}").exists()


def test_cleanup_retry_never_touches_new_registration(registry, monkeypatch):
    session, root, client = registry
    project, _, _, component, artifact = published(session, root)
    identifier = artifact.id
    stage = root / "artifacts" / f".delete-{identifier}"
    original = shutil.rmtree
    def failed(path, *args, **kwargs):
        if Path(path) == stage / "artifact":
            raise PermissionError("temporary cleanup failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(deletion.shutil, "rmtree", failed)
    response = client.delete(f"/api/v1/artifacts/{identifier}")
    assert response.status_code == 503 and response.json()["detail"]["deleted"] is True
    replacement = register_file(session, Path(component["wheel"]), project, scheme_version="1.2.0")
    session.commit()
    monkeypatch.setattr(deletion.shutil, "rmtree", original)
    assert client.delete(f"/api/v1/artifacts/{identifier}").status_code == 204
    assert artifact_path(replacement).is_file()
    assert replacement.storage_directory != artifact.storage_directory


def test_cleanup_retry_after_final_rmdir_failure_preserves_new_registration(registry, monkeypatch):
    session, root, client = registry
    project, _, _, component, artifact = published(session, root)
    identifier = artifact.id
    stage = root / "artifacts" / f".delete-{identifier}"
    original = Path.rmdir
    attempts = 0

    def failed_once(path):
        nonlocal attempts
        if path == stage:
            attempts += 1
            if attempts == 1:
                raise PermissionError("temporary final rmdir failure")
        return original(path)

    monkeypatch.setattr(Path, "rmdir", failed_once)
    response = client.delete(f"/api/v1/artifacts/{identifier}")
    assert response.status_code == 503 and response.json()["detail"]["deleted"] is True
    assert attempts == 1 and stage.is_dir() and not any(stage.iterdir())
    with Session(session.bind) as current:
        assert current.get(ResearchArtifact, identifier) is None
    replacement = register_file(session, Path(component["wheel"]), project, scheme_version="1.2.0")
    session.commit()
    assert client.delete(f"/api/v1/artifacts/{identifier}").status_code == 204
    assert attempts == 2 and not stage.exists()
    assert artifact_path(replacement).is_file() and Path(component["wheel"]).is_file()
    assert replacement.storage_directory != artifact.storage_directory


def test_empty_stage_does_not_bypass_existing_artifact_ownership(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    stage = root / "artifacts" / f".delete-{artifact.id}"
    stage.mkdir()
    response = client.delete(f"/api/v1/artifacts/{artifact.id}")
    assert response.status_code == 409, response.text
    assert stage.is_dir() and artifact_path(artifact).is_file()
    assert session.get(ResearchArtifact, artifact.id) is artifact


@pytest.mark.parametrize("foreign", ["marker", "unknown-file", "storage-path", "missing-marker"])
def test_foreign_stage_is_never_removed(registry, foreign):
    _, root, client = registry
    identifier = uuid4()
    stage = root / "artifacts" / f".delete-{identifier}"
    payload = stage / "artifact"
    payload.mkdir(parents=True)
    (payload / "unowned").write_text("preserve")
    marker = {"artifact_id": str(uuid4()) if foreign == "marker" else str(identifier),
              "storage_directory": "../foreign" if foreign == "storage-path" else str(identifier)}
    if foreign != "missing-marker":
        (stage / "metadata.json").write_text(json.dumps(marker))
    if foreign == "unknown-file":
        (stage / "unowned").write_text("preserve")
    response = client.delete(f"/api/v1/artifacts/{identifier}")
    assert response.status_code == 409, response.text
    assert stage.exists() and (payload / "unowned").is_file()


@pytest.mark.parametrize("failure", ["unlock", "invalidate", "close"])
def test_deletion_guard_cleans_process_lock_after_connection_failure(failure):
    identifier = uuid4()
    session = Mock()
    session.get_bind.return_value.dialect.name = "postgresql"
    connection = session.get_bind.return_value.connect.return_value
    connection.scalar.return_value = True
    if failure in {"unlock", "invalidate"}:
        connection.execute.side_effect = RuntimeError("unlock")
    if failure == "invalidate":
        connection.invalidate.side_effect = RuntimeError("invalidate")
    elif failure == "close":
        connection.close.side_effect = RuntimeError("close")
    with pytest.raises(RuntimeError, match=failure):
        with deletion._deletion_guard(identifier, session):
            assert identifier in deletion._deleting
    assert identifier not in deletion._deleting
    if failure in {"unlock", "invalidate"}:
        connection.invalidate.assert_called_once()
    connection.close.assert_called_once()
    if failure == "unlock":
        assert [call[0] for call in connection.method_calls] == ["scalar", "execute", "invalidate", "close"]
    local = Mock()
    local.get_bind.return_value.dialect.name = "sqlite"
    with deletion._deletion_guard(identifier, local):
        pass
    assert identifier not in deletion._deleting


def test_unknown_and_unpublished_are_not_deleted(registry):
    session, root, client = registry
    assert client.delete(f"/api/v1/artifacts/{uuid4()}").status_code == 404
    project = make_project(session, root)
    version, _, _ = make_run(session, root, project)
    identifier = version.accepted_artifacts[project.package_name]["id"]
    assert client.delete(f"/api/v1/artifacts/{identifier}").status_code == 422
