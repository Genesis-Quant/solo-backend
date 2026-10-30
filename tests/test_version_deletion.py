import json
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from config import DolphinSchedulerSettings, SoloSettings
from core.apps.artifacts.models import ResearchArtifact
from core.apps.artifacts.service import artifact_snapshot
from core.apps.projects import version_deletion as cleanup
from core.apps.projects.deletion import delete_version
from core.apps.projects.models import Project, ProjectVersion, VersionDeletion
from core.apps.tasks.models import TaskAttempt
from core.database.base import Base
from core.scheduler.client import DolphinSchedulerBusyError, DolphinSchedulerError


@pytest.fixture
def environment(tmp_path, monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(DolphinSchedulerSettings, "ENABLED", True)
    monkeypatch.setattr(cleanup, "acquire_lease", Mock())
    monkeypatch.setattr(cleanup, "release_lease", Mock())
    scheduler = Mock()
    scheduler.owned_instances.return_value = []
    scheduler.__enter__ = Mock(return_value=scheduler)
    scheduler.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(cleanup, "DolphinSchedulerClient", lambda: scheduler)
    project = Project(name="version-delete-test", kind="factor", template_tag="v1.12.3",
                      template_commit="a" * 40, template_package_version="1.12.3",
                      scheme_version="v1.12.8", scheme_commit="b" * 40,
                      artifact_dependencies={}, description="", version_sequence=3)
    session.add(project)
    session.commit()
    yield session, project, scheduler, tmp_path
    session.close()
    engine.dispose()


def saved_version(session, project, root, *, status="failed", number=3, workflow_id=None):
    version = ProjectVersion(project_id=project.id, number=number, package_version=f"1.12.{number}",
                             status=status, parameters={}, accepted_artifacts={}, scheme_version="1.12.8",
                             workflow_id=workflow_id, error="", attempt=1)
    session.add(version)
    session.flush()
    attempt = TaskAttempt(run_id=version.id, number=1, kind=project.kind, status=status, tasks=[],
                          workflow_id=workflow_id, error="", sync_error="", created_at=datetime.now(UTC))
    session.add(attempt)
    session.commit()
    run = root / "runs" / str(version.id)
    (run / "environment").mkdir(parents=True)
    (run / "input.json").write_text(json.dumps({"kind": project.kind}), encoding="utf-8")
    (run / "environment" / "input-data").write_bytes(b"frozen" * 100)
    return version, run


def candidate(session, project, root, version, *, published=False):
    identifier = uuid4()
    filename = f"{project.package_name.replace('-', '_')}-{version.package_version}-py3-none-any.whl"
    directory = root / "artifacts" / str(identifier)
    directory.mkdir(parents=True)
    (directory / filename).write_bytes(b"candidate")
    artifact = ResearchArtifact(id=identifier, kind="factor", package_name=project.package_name,
                                version=version.package_version, storage_directory=str(identifier),
                                filename=filename, size_bytes=9, entry="", sources={}, dependencies={},
                                scheme_version="1.12.8", origin_project_id=project.id,
                                origin_version_id=version.id,
                                published_at=datetime.now(UTC) if published else None)
    session.add(artifact)
    session.flush()
    version.accepted_artifacts = {artifact.package_name: artifact_snapshot(artifact)}
    session.commit()
    return artifact, directory


def test_success_reclaims_only_the_selected_run_and_unused_candidates(environment):
    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root, workflow_id=12)
    artifact, storage = candidate(session, project, root, version)
    other, other_run = saved_version(session, project, root, number=2)
    notebook = root / "projects" / "factor" / project.name / "reports" / "original.json"
    notebook.parent.mkdir(parents=True)
    notebook.write_text("keep", encoding="utf-8")
    instance = {"id": 12, "kind": "factor"}
    scheduler.owned_instances.return_value = [instance]
    result = delete_version(project.id, version.id, session)
    assert result["cleanupPending"] is False and result["freedBytes"] > 600
    assert session.get(ProjectVersion, version.id) is None
    assert not session.scalars(select(TaskAttempt).where(TaskAttempt.run_id == version.id)).all()
    assert not run.exists() and not storage.exists()
    assert session.get(ResearchArtifact, artifact.id) is None
    assert session.get(ProjectVersion, other.id) and other_run.is_dir() and notebook.read_text() == "keep"
    assert session.get(VersionDeletion, version.id) is None
    assert project.version_sequence == 3
    scheduler.delete_owned_instance.assert_called_once_with(instance)
    cleanup.release_lease.assert_called_once()


@pytest.mark.parametrize("status", ["building", "queued", "running", "stopping", "retrying"])
def test_active_version_is_not_deleted(environment, status):
    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root, status=status)
    with pytest.raises(HTTPException) as caught:
        delete_version(project.id, version.id, session)
    assert caught.value.status_code == 409 and run.exists()
    assert session.get(ProjectVersion, version.id)
    scheduler.delete_owned_instance.assert_not_called()


def test_published_artifact_blocks_deletion(environment):
    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root, status="success")
    artifact, storage = candidate(session, project, root, version, published=True)
    with pytest.raises(HTTPException, match="已发布"):
        delete_version(project.id, version.id, session)
    assert run.exists() and storage.exists() and session.get(ResearchArtifact, artifact.id)
    scheduler.delete_owned_instance.assert_not_called()


@pytest.mark.parametrize("kind", ["busy", "unavailable"])
def test_scheduler_validation_failure_keeps_version_and_run(environment, kind):
    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root, workflow_id=12)
    error = DolphinSchedulerBusyError("busy") if kind == "busy" else DolphinSchedulerError("offline")
    scheduler.owned_instances.side_effect = error
    with pytest.raises(HTTPException) as caught:
        delete_version(project.id, version.id, session)
    assert caught.value.status_code == (409 if kind == "busy" else 503)
    assert run.exists() and session.get(ProjectVersion, version.id)
    assert session.get(VersionDeletion, version.id).payload["phase"] == "preparing"
    scheduler.owned_instances.side_effect = None
    assert delete_version(project.id, version.id, session)["cleanupPending"] is False


def test_scheduler_cleanup_failure_is_retryable_after_record_deletion(environment):
    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root, workflow_id=12)
    scheduler.owned_instances.return_value = [{"id": 12}]
    scheduler.delete_owned_instance.side_effect = DolphinSchedulerError("offline")
    result = delete_version(project.id, version.id, session)
    assert result["cleanupPending"] and result["freedBytes"] == 0
    assert not session.get(ProjectVersion, version.id) and not run.exists()
    assert session.get(VersionDeletion, version.id).payload["phase"] == "deleted"
    scheduler.delete_owned_instance.side_effect = None
    assert not delete_version(project.id, version.id, session)["cleanupPending"]
    assert session.get(VersionDeletion, version.id) is None


def test_rmtree_failure_is_retryable_without_deleting_another_version(environment, monkeypatch):
    session, project, _, root = environment
    version, run = saved_version(session, project, root)
    real = cleanup.shutil.rmtree
    monkeypatch.setattr(cleanup.shutil, "rmtree", Mock(side_effect=OSError("disk busy")))
    result = delete_version(project.id, version.id, session)
    assert result["cleanupPending"] and result["freedBytes"] == 0
    job = session.get(VersionDeletion, version.id)
    assert (cleanup.paths(job)[1] / "run").is_dir()
    monkeypatch.setattr(cleanup.shutil, "rmtree", real)
    result = delete_version(project.id, version.id, session)
    assert not result["cleanupPending"] and result["freedBytes"] > 600
    assert not run.exists() and not list(run.parent.glob(".delete-version-*"))


def test_db_rollback_restores_run_before_retry(environment, monkeypatch):
    session, project, _, root = environment
    version, run = saved_version(session, project, root)
    commit = session.commit

    def fail_after_staging():
        if any(isinstance(obj, ProjectVersion) for obj in session.deleted):
            raise OSError("commit failed")
        return commit()

    monkeypatch.setattr(session, "commit", fail_after_staging)
    with pytest.raises(HTTPException) as caught:
        delete_version(project.id, version.id, session)
    assert caught.value.status_code == 503 and run.is_dir()
    assert session.get(ProjectVersion, version.id)
    monkeypatch.setattr(session, "commit", commit)
    assert not delete_version(project.id, version.id, session)["cleanupPending"]


def test_lost_commit_ack_does_not_restore_deleted_run(environment, monkeypatch):
    session, project, _, root = environment
    version, run = saved_version(session, project, root)
    commit = session.commit

    def lost_ack():
        deleting = any(isinstance(obj, ProjectVersion) for obj in session.deleted)
        commit()
        if deleting:
            raise OSError("lost response")

    monkeypatch.setattr(session, "commit", lost_ack)
    result = delete_version(project.id, version.id, session)
    assert result["cleanupPending"] and not run.exists()
    assert session.get(ProjectVersion, version.id) is None
    monkeypatch.setattr(session, "commit", commit)
    assert not delete_version(project.id, version.id, session)["cleanupPending"]


def test_shared_candidate_and_unrelated_orphan_are_retained(environment):
    session, project, _, root = environment
    version, _ = saved_version(session, project, root)
    shared, shared_path = candidate(session, project, root, version)
    other, _ = saved_version(session, project, root, number=2)
    orphan, orphan_path = candidate(session, project, root, other)
    other.accepted_artifacts = {shared.package_name: artifact_snapshot(shared)}
    session.commit()
    delete_version(project.id, version.id, session)
    assert session.get(ResearchArtifact, shared.id) and shared_path.exists()
    assert session.get(ResearchArtifact, orphan.id) and orphan_path.exists()


def test_jupyter_busy_lease_never_releases_someone_elses_gate(environment):
    session, project, _, root = environment
    version, run = saved_version(session, project, root)
    cleanup.acquire_lease.side_effect = HTTPException(409, "busy")
    with pytest.raises(HTTPException) as caught:
        delete_version(project.id, version.id, session)
    assert caught.value.status_code == 409 and run.exists()
    cleanup.release_lease.assert_not_called()


def test_lease_release_failure_retains_cleanup_for_original_id(environment):
    session, project, _, root = environment
    version, _ = saved_version(session, project, root)
    cleanup.release_lease.side_effect = HTTPException(503, "release failed")
    assert delete_version(project.id, version.id, session)["cleanupPending"]
    assert session.get(VersionDeletion, version.id).payload["phase"] == "complete"
    cleanup.release_lease.side_effect = None
    assert not delete_version(project.id, version.id, session)["cleanupPending"]


def test_foreign_project_cannot_delete_or_retry_cleanup(environment):
    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root)
    with pytest.raises(HTTPException) as caught:
        delete_version(uuid4(), version.id, session)
    assert caught.value.status_code == 404 and run.exists()
    scheduler.delete_owned_instance.assert_not_called()


def test_unowned_staging_directory_is_not_purged(environment):
    session, project, _, root = environment
    version, run = saved_version(session, project, root)
    stage = run.parent / f".delete-version-{version.id}"
    stage.mkdir()
    (stage / "foreign-file").write_text("keep", encoding="utf-8")
    with pytest.raises(HTTPException):
        delete_version(project.id, version.id, session)
    assert (stage / "foreign-file").read_text() == "keep" and run.exists()


def test_symlink_to_another_run_is_rejected(environment):
    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root)
    target = root / "runs" / "foreign"
    run.rename(target)
    try:
        run.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks requires Windows developer privileges")
    with pytest.raises(HTTPException):
        delete_version(project.id, version.id, session)
    assert (target / "environment" / "input-data").read_bytes() == b"frozen" * 100
    assert session.get(ProjectVersion, version.id)
    scheduler.delete_owned_instance.assert_not_called()


def test_pending_cleanup_survives_a_new_database_session(environment):
    session, project, scheduler, root = environment
    version, _ = saved_version(session, project, root, workflow_id=12)
    scheduler.owned_instances.return_value = [{"id": 12}]
    scheduler.delete_owned_instance.side_effect = DolphinSchedulerError("offline")
    project_id, version_id = project.id, version.id
    assert delete_version(project_id, version_id, session)["cleanupPending"]
    scheduler.delete_owned_instance.side_effect = None
    with sessionmaker(bind=session.get_bind(), autoflush=False, expire_on_commit=False)() as next_session:
        assert next_session.get(VersionDeletion, version_id)
        assert not delete_version(project_id, version_id, next_session)["cleanupPending"]
        assert next_session.get(VersionDeletion, version_id) is None


def test_project_response_does_not_expose_deleted_versions_or_cleanup(environment):
    from core.apps.projects.views import read_project

    session, project, scheduler, root = environment
    version, _ = saved_version(session, project, root, workflow_id=12)
    scheduler.owned_instances.return_value = [{"id": 12}]
    scheduler.delete_owned_instance.side_effect = DolphinSchedulerError("offline")
    assert delete_version(project.id, version.id, session)["cleanupPending"]
    session.expire_all()
    response = read_project(session.get(Project, project.id))
    assert not response.versions
    assert "versionCleanups" not in response.model_dump()
    assert str(version.id) not in json.dumps(response.model_dump(mode="json"))


def test_background_worker_reclaims_deleted_versions_without_web_entry(environment, monkeypatch):
    from core.apps.projects import version_cleanup

    session, project, scheduler, root = environment
    version, _ = saved_version(session, project, root, workflow_id=12)
    scheduler.owned_instances.return_value = [{"id": 12}]
    scheduler.delete_owned_instance.side_effect = DolphinSchedulerError("offline")
    assert delete_version(project.id, version.id, session)["cleanupPending"]
    scheduler.delete_owned_instance.side_effect = None
    monkeypatch.setattr(version_cleanup, "database_session_factory", lambda: sessionmaker(
        bind=session.get_bind(), autoflush=False, expire_on_commit=False))
    version_cleanup.retry_version_cleanups()
    session.expire_all()
    assert session.get(VersionDeletion, version.id) is None
    assert not list((root / "runs").glob(".delete-version-*"))


def test_background_worker_does_not_finish_unaccepted_deletions(environment, monkeypatch):
    from core.apps.projects import version_cleanup

    session, project, scheduler, root = environment
    version, run = saved_version(session, project, root)
    cleanup.acquire_lease.side_effect = HTTPException(409, "busy")
    with pytest.raises(HTTPException):
        delete_version(project.id, version.id, session)
    monkeypatch.setattr(version_cleanup, "database_session_factory", lambda: sessionmaker(
        bind=session.get_bind(), autoflush=False, expire_on_commit=False))
    version_cleanup.retry_version_cleanups()
    assert session.get(ProjectVersion, version.id) and run.exists()
    scheduler.delete_owned_instance.assert_not_called()


def test_monotonic_counter_is_preserved_even_for_legacy_rows(environment):
    session, project, _, root = environment
    project.version_sequence = 0
    version, _ = saved_version(session, project, root, number=7)
    delete_version(project.id, version.id, session)
    assert session.get(Project, project.id).version_sequence == 7
