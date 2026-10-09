"""Publication transaction recovery uses only memory SQLite and temporary files."""

import hashlib
import json
import socket
import subprocess
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps.artifacts import service
from core.apps.artifacts.models import ResearchArtifact
from core.apps.projects.models import Project, ProjectVersion
from core.database.base import Base
from test_artifacts import make_project, make_run, make_wheel


@pytest.fixture
def registry(tmp_path, monkeypatch):
    # Service-level recovery needs no TestClient or event-loop self-pipe sockets.
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    with Session(engine, expire_on_commit=False) as session:
        yield session, tmp_path, None
    engine.dispose()


@pytest.fixture(autouse=True)
def no_external_resources(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Publication recovery tests must not use external resources")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)


def saved_publication(registry):
    session, root, _ = registry
    project = make_project(session, root)
    version, directory, _ = make_run(session, root, project)
    identifier = UUID(version.accepted_artifacts[project.package_name]["id"])
    artifact = service.require_artifact(session, identifier)
    bundle = service.artifact_path(artifact).parent / "bundle"
    return session, project, version, directory, artifact, bundle


def tree(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()}


def protect_final_bundle(monkeypatch, bundle):
    original = service.shutil.rmtree

    def remove(path, *args, **kwargs):
        assert Path(path) != bundle, "Recovery must never blindly delete a final bundle"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(service.shutil, "rmtree", remove)


@pytest.mark.parametrize("commit_error", [OperationalError("COMMIT", {}, RuntimeError("before commit")),
                                         OSError("commit did not reach the database")])
def test_commit_failure_before_database_commit_retains_exact_retryable_bundle(registry, monkeypatch, commit_error):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    identifier = artifact.id
    original_run = tree(directory)
    wheel = service.artifact_path(artifact).read_bytes()
    accepted = deepcopy(version.accepted_artifacts)
    hashes = version.input_sha256, version.lock_sha256
    commit = session.commit

    def fail_commit():
        raise commit_error

    protect_final_bundle(monkeypatch, bundle)
    monkeypatch.setattr(session, "commit", fail_commit)
    with pytest.raises(type(commit_error)):
        service.publish_version_committed(session, project, version)
    persisted = session.execute(select(ResearchArtifact.published_at, ResearchArtifact.bundle_path,
        ResearchArtifact.bundle_manifest_sha256).where(ResearchArtifact.id == identifier)).one()
    assert persisted == (None, None, None)
    assert bundle.is_dir()
    before = tree(bundle)
    seal = hashlib.sha256(before["manifest.json"]).hexdigest()
    monkeypatch.setattr(session, "commit", commit)
    retried = service.publish_version_committed(session, project, version)
    assert retried.id == identifier and retried.bundle_manifest_sha256 == seal
    assert service.verify_bundle(retried)[0] == bundle
    assert tree(bundle) == before
    assert tree(directory) == original_run
    assert service.artifact_path(retried).read_bytes() == wheel
    assert version.accepted_artifacts == accepted
    assert (version.input_sha256, version.lock_sha256) == hashes
    assert not list(bundle.parent.glob(f".bundle-{identifier}-*"))


@pytest.mark.parametrize("commit_error", [OperationalError("COMMIT", {}, RuntimeError("ACK lost")),
                                         OSError("database committed but acknowledgement was lost")])
def test_lost_commit_ack_returns_database_sealed_bundle_without_deleting_it(registry, monkeypatch, commit_error):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    commit = session.commit
    attempts = []
    protect_final_bundle(monkeypatch, bundle)

    def lost_ack():
        attempts.append(True)
        commit()
        raise commit_error

    monkeypatch.setattr(session, "commit", lost_ack)
    published = service.publish_version_committed(session, project, version)
    assert len(attempts) == 1
    seal, published_at = published.bundle_manifest_sha256, published.published_at
    before = tree(bundle)
    assert service.verify_bundle(published)[0] == bundle
    service.shutil.rmtree(directory)
    monkeypatch.setattr(session, "commit", commit)
    retried = service.publish_version_committed(session, project, version)
    assert retried.id == published.id
    assert retried.published_at == published_at
    assert retried.bundle_manifest_sha256 == seal
    assert tree(bundle) == before
    assert service.artifact_component(session, retried.id)["published"] is True


@pytest.mark.parametrize("committed", [False, True])
def test_unknown_commit_truth_retains_bundle_and_recovers_on_next_request(registry, monkeypatch, committed):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    commit, lock = session.commit, service.lock_registry
    commit_started = False
    protect_final_bundle(monkeypatch, bundle)

    def uncertain_commit():
        nonlocal commit_started
        if committed:
            commit()
        commit_started = True
        raise OperationalError("COMMIT", {}, RuntimeError("connection lost"))

    def unavailable_truth(current):
        if commit_started:
            raise OperationalError("SELECT", {}, RuntimeError("database still unavailable"))
        return lock(current)

    monkeypatch.setattr(session, "commit", uncertain_commit)
    monkeypatch.setattr(service, "lock_registry", unavailable_truth)
    with pytest.raises(service.ArtifactError) as failed:
        service.publish_version_committed(session, project, version)
    assert failed.value.code == "artifact_publish_state_unknown"
    before = tree(bundle)
    monkeypatch.setattr(session, "commit", commit)
    monkeypatch.setattr(service, "lock_registry", lock)
    if committed:
        service.shutil.rmtree(directory)
    published = service.publish_version_committed(session, project, version)
    assert service.verify_bundle(published)[0] == bundle
    assert tree(bundle) == before


@pytest.mark.parametrize("failure", ["rollback", "session_close"])
def test_caller_rollback_or_crash_close_is_recovered_by_fresh_session(registry, monkeypatch, failure):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    project_id, version_id, identifier = project.id, version.id, artifact.id
    bind = session.get_bind()
    session.rollback()
    protect_final_bundle(monkeypatch, bundle)
    with Session(bind, expire_on_commit=False) as first:
        service.publish_version(first, first.get(Project, project_id), first.get(ProjectVersion, version_id))
        if failure == "rollback":
            first.rollback()
        # Closing an uncommitted session models recovery in another worker after
        # process death; no in-memory callback or transaction receipt is reused.
    before = tree(bundle)
    with Session(bind, expire_on_commit=False) as retry:
        row = retry.get(ResearchArtifact, identifier)
        assert row.published_at is None and row.bundle_manifest_sha256 is None
        published = service.publish_version_committed(retry, retry.get(Project, project_id),
            retry.get(ProjectVersion, version_id))
        assert service.verify_bundle(published)[0] == bundle
    assert tree(bundle) == before


def test_flush_failure_keeps_owned_final_bundle_for_retry(registry, monkeypatch):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    protect_final_bundle(monkeypatch, bundle)

    def fail_publication_flush(current, context, instances):
        if any(isinstance(item, ResearchArtifact) and item.published_at is not None for item in current.dirty):
            raise ValueError("publication seal flush failed")

    event.listen(session, "before_flush", fail_publication_flush)
    try:
        with pytest.raises(ValueError, match="seal flush failed"):
            service.publish_version_committed(session, project, version)
    finally:
        event.remove(session, "before_flush", fail_publication_flush)
    before = tree(bundle)
    published = service.publish_version_committed(session, project, version)
    assert service.verify_bundle(published)[0] == bundle
    assert tree(bundle) == before


def test_failure_after_atomic_rename_does_not_delete_final_bundle(registry, monkeypatch):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    sync = service._sync_bundle_directory
    protect_final_bundle(monkeypatch, bundle)

    def fail_after_rename(path):
        if path == bundle.parent:
            raise OSError("directory flush acknowledgement lost after rename")
        return sync(path)

    monkeypatch.setattr(service, "_sync_bundle_directory", fail_after_rename)
    with pytest.raises(OSError, match="after rename"):
        service.publish_version_committed(session, project, version)
    before = tree(bundle)
    monkeypatch.setattr(service, "_sync_bundle_directory", sync)
    published = service.publish_version_committed(session, project, version)
    assert service.verify_bundle(published)[0] == bundle
    assert tree(bundle) == before


@pytest.mark.parametrize("tamper", ["malformed_manifest", "other_uuid", "input_and_manifest", "extra_file", "extra_directory"])
def test_recovery_rejects_nonexact_or_foreign_bundle_without_cleanup(registry, monkeypatch, tamper):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    identifier, wheel_path = artifact.id, service.artifact_path(artifact)
    wheel = wheel_path.read_bytes()
    service.publish_version(session, project, version)
    session.rollback()
    manifest_path = bundle / "manifest.json"
    if tamper == "malformed_manifest":
        manifest_path.write_text("{broken", encoding="utf-8")
    elif tamper == "other_uuid":
        manifest = json.loads(manifest_path.read_bytes())
        manifest["artifact_id"] = str(uuid4())
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "input_and_manifest":
        input_path = bundle / "input.json"
        input_path.write_text('{"kind":"model","tampered":true}', encoding="utf-8")
        manifest = json.loads(manifest_path.read_bytes())
        manifest["files"]["input.json"] = hashlib.sha256(input_path.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif tamper == "extra_file":
        (bundle / "unknown-owned-by-someone-else").write_text("do not delete", encoding="utf-8")
    else:
        (bundle / "unknown-empty-directory").mkdir()
    before = tree(bundle)
    protect_final_bundle(monkeypatch, bundle)
    for _ in range(2):
        with pytest.raises(service.ArtifactError, match="not the exact accepted recovery tree"):
            service.publish_version_committed(session, project, version)
        assert tree(bundle) == before
        assert wheel_path.read_bytes() == wheel
        assert session.get(ResearchArtifact, identifier).published_at is None
    if tamper == "extra_directory":
        assert (bundle / "unknown-empty-directory").is_dir()


def test_legacy_manifest_order_is_adopted_without_rewriting_any_bytes(registry):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    service.publish_version(session, project, version)
    session.rollback()
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    legacy = {key: manifest[key] for key in reversed(manifest)}
    manifest_path.write_text(json.dumps(legacy, ensure_ascii=False, indent=4), encoding="utf-8")
    before = tree(bundle)
    published = service.publish_version_committed(session, project, version)
    assert tree(bundle) == before
    assert published.bundle_manifest_sha256 == hashlib.sha256(before["manifest.json"]).hexdigest()
    assert service.verify_bundle(published)[0] == bundle


def test_recovery_does_not_scan_or_clean_other_uuid_or_malformed_stages(registry):
    session, project, version, directory, artifact, bundle = saved_publication(registry)
    for name in (f".bundle-{uuid4()}-foreign", ".bundle-malformed"):
        foreign = bundle.parent / name
        foreign.mkdir()
        (foreign / "manifest.json").write_text('{"do-not-adopt":true}', encoding="utf-8")
    before = {name: tree(bundle.parent / name) for name in (path.name for path in bundle.parent.glob(".bundle-*"))}
    service.publish_version_committed(session, project, version)
    for name, content in before.items():
        assert tree(bundle.parent / name) == content


def test_retry_retains_accepted_upstream_snapshot_after_upstream_seals_its_own_graph(registry):
    session, root, _ = registry
    upstream = make_project(session, root, kind="factor")
    unused = service.register_asset(session, make_wheel(root / "assets/unused.whl", "unused-helper",
        "2.0.0", requires_scheme=False))
    upstream.artifact_dependencies = {unused.package_name: service.artifact_snapshot(unused)}
    session.commit()
    upstream_version, _, _ = make_run(session, root, upstream)
    selected = service.require_artifact(session, upstream_version.accepted_artifacts[upstream.package_name])
    consumer = make_project(session, root)
    version, _, _ = make_run(session, root, consumer,
        assets={selected.package_name: service.artifact_path(selected)})
    accepted = deepcopy(version.accepted_artifacts)
    publication = service.publish_version(session, consumer, version)
    bundle = Path(publication.bundle_path)
    before = tree(bundle)
    session.rollback()
    service.publish_version_committed(session, upstream, upstream_version)
    assert selected.dependencies == {}
    retried = service.publish_version_committed(session, consumer, version)
    assert retried.dependencies[selected.package_name] == accepted[selected.package_name]
    assert version.accepted_artifacts == accepted
    assert tree(bundle) == before
    assert service.artifact_component(session, retried.id)["published"] is True
