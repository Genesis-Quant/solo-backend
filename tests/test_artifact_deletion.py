"""Published deletion with real SQLite, owned wheel/bundle bytes, and HTTP calls.

All fixtures use temporary storage. No live shared directory, scheduler, or API.
"""

import hashlib
import json
import os
import shutil
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import tomlkit
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.artifacts import deletion
from core.apps.artifacts.models import ArtifactInstallation, ResearchArtifact
from core.apps.artifacts.service import (artifact_component, artifact_path, artifact_snapshot,
    publish_version, register_file, require_artifact)
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies.models import Strategy
from test_artifacts import make_project, make_run, make_wheel, registry


def published(session, root, *, project=None, assets=None):
    project = project or make_project(session, root)
    version, run, component = make_run(session, root, project, assets=assets)
    artifact = publish_version(session, project, version)
    session.commit()
    return project, version, run, component, artifact


def bytes_in(directory):
    return {path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file() and not path.is_symlink()}


def own_stage(root, artifact):
    return root / "artifacts" / f".delete-{artifact.id}-{artifact.sha256}"


def delete_url(artifact):
    return f"/api/v1/artifacts/{artifact.id}"


def assert_in_use(response, expected_type):
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "artifact_in_use"
    assert "不能删除" in detail["reason"]
    assert any(item["type"] == expected_type for item in detail["refs"])


def test_delete_published_row_entire_own_sha_dir_preserves_source_version_run_and_report(registry):
    session, root, client = registry
    project, version, run, component, artifact = published(session, root)
    identifier, version_id = artifact.id, version.id
    own = artifact_path(artifact).parent
    # Inspection may have created this derivative environment, not in bundle seal.
    venv = own / "bundle/environment/.venv/lib"
    venv.mkdir(parents=True)
    (venv / "installed.bin").write_bytes(b"registry environment only")
    (own / "old-bundle.env").write_text("owned stale derivative")
    source = policy.project_path(project)
    candidate = make_wheel(source / "dist/candidate.whl", project.package_name, contents="candidate = 1")
    consumer_copy = root / "consumer-copy.whl"
    shutil.copyfile(Path(component["wheel"]), consumer_copy)
    before_source, before_run = bytes_in(source), bytes_in(run)
    before_snapshot = deepcopy(version.accepted_artifacts)
    wheel_bytes = Path(component["wheel"]).read_bytes()
    report_bytes = (run / "report/run.json").read_bytes()
    assert client.get(delete_url(artifact) + "/wheel").status_code == 200

    response = client.delete(delete_url(artifact))

    assert response.status_code == 204, response.text
    assert response.content == b""
    with Session(session.bind) as saved:
        assert saved.get(ResearchArtifact, identifier) is None
        saved_version = saved.get(ProjectVersion, version_id)
        assert saved_version is not None and saved_version.accepted_artifacts == before_snapshot
        assert saved.get(Project, project.id) is not None
    assert not own.exists() and not own_stage(root, artifact).exists()
    assert bytes_in(source) == before_source and bytes_in(run) == before_run
    assert candidate.is_file() and consumer_copy.read_bytes() == wheel_bytes
    assert (run / "report/run.json").read_bytes() == report_bytes
    assert client.get(delete_url(artifact)).status_code == 404
    assert client.get(delete_url(artifact) + "/wheel").status_code == 404
    assert client.delete(delete_url(artifact)).status_code == 404
    # SQLite loses DateTime timezone metadata; preserve this test's real row
    # timestamps without changing PostgreSQL-only historical rendering code.
    version.created_at = version.created_at.replace(tzinfo=UTC)
    version.finished_at = datetime.now(UTC)
    versions = client.get(f"/api/v1/projects/{project.id}/versions")
    assert versions.status_code == 200, versions.text
    result = next(item for item in versions.json() if item["id"] == str(version_id))
    assert result["reportPath"] == f"{version_id}/report"
    assert result["publishStatus"] == "unpublished" and result["artifactId"] is None and result["files"] == []
    policy.check_task(session, version, project=project, directory=run, kind=project.kind)
    # Historical evidence survives; new publication cannot resurrect old identity.
    assert client.post(f"/api/v1/projects/{project.id}/versions/{version_id}/publish", json={}).status_code == 404


@pytest.mark.parametrize("published_consumer", [True, False])
def test_other_artifact_exact_closure_protects_published_and_unpublished_consumers(registry, published_consumer):
    session, root, client = registry
    source, _, _, _, artifact = published(session, root, project=make_project(session, root, kind="factor"))
    consumer = make_project(session, root)
    if published_consumer:
        _, _, _, _, other = published(session, root, project=consumer,
                                     assets={artifact.package_name: artifact_path(artifact)})
    else:
        wheel = make_wheel(policy.project_path(consumer) / "candidate.whl", consumer.package_name)
        other = register_file(session, wheel, consumer, scheme_version="1.2.0",
            component={"dependencies": {"nested": {"snapshots": [artifact_snapshot(artifact)]}}})
        session.commit()
    before_artifact = bytes_in(artifact_path(artifact).parent)
    before_other = bytes_in(artifact_path(other).parent)
    before_dependencies = deepcopy(other.dependencies)

    response = client.delete(delete_url(artifact))

    assert_in_use(response, "artifact")
    assert ("已发布" if published_consumer else "未发布") in response.json()["detail"]["reason"]
    assert str(other.id) in response.json()["detail"]["reason"]
    assert other.package_name in response.json()["detail"]["reason"]
    assert require_artifact(session, artifact.id)
    assert other.dependencies == before_dependencies
    assert bytes_in(artifact_path(artifact).parent) == before_artifact
    assert bytes_in(artifact_path(other).parent) == before_other


@pytest.mark.parametrize("reference", ["id", "sha", "nested", "unrelated_package"])
def test_current_project_dependencies_use_exact_id_or_digest_not_package_name(registry, reference):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    consumer = make_project(session, root)
    snapshot = artifact_snapshot(artifact)
    if reference == "id":
        ref = {"artifact_id": str(artifact.id)}
    elif reference == "sha":
        ref = {"package": artifact.package_name, "sha256": artifact.sha256}
    elif reference == "nested":
        ref = {"closure": [snapshot]}
    else:
        ref = {**snapshot, "artifact_id": str(uuid4()), "id": str(uuid4()), "sha256": "0" * 64}
    consumer.artifact_dependencies = {artifact.package_name: ref}
    session.commit()
    before = deepcopy(consumer.artifact_dependencies)
    old_bytes = bytes_in(artifact_path(artifact).parent)

    response = client.delete(delete_url(artifact))

    if reference == "unrelated_package":
        assert response.status_code == 204, response.text
    else:
        assert_in_use(response, "project")
        assert str(consumer.id) in response.json()["detail"]["reason"]
        assert consumer.name in response.json()["detail"]["reason"]
        assert bytes_in(artifact_path(artifact).parent) == old_bytes
    assert consumer.artifact_dependencies == before


@pytest.mark.parametrize("phase", ["before", "pending", "accepted"])
@pytest.mark.parametrize("expired", [False, True])
def test_installation_protects_accepted_receipts_until_explicit_finalization(registry, phase, expired):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    consumer = make_project(session, root)
    snapshots = {artifact.package_name: artifact_snapshot(artifact)}
    receipt = {"format": 1, "before": {}, "pending": {}, "accepted": None}
    receipt[phase] = {str(artifact.id): artifact_snapshot(artifact)} if phase == "pending" else snapshots
    installation = ArtifactInstallation(id=uuid4(), consumer_project_id=consumer.id, artifacts=receipt,
        expires_at=datetime.now(UTC) + timedelta(minutes=-1 if expired else 5))
    session.add(installation)
    session.commit()
    before = deepcopy(installation.artifacts)

    response = client.delete(delete_url(artifact))

    if expired and phase != "accepted":
        assert response.status_code == 204, response.text
    else:
        assert_in_use(response, "installation")
        assert artifact_path(artifact).is_file()
    assert session.get(ArtifactInstallation, installation.id).artifacts == before
    assert consumer.artifact_dependencies == {}


def accepted_strategy(session, root, artifact, *, status):
    strategy = Strategy(id=uuid4(), name="own immutable wheel copies", forms={}, status=status,
        components={artifact.kind: {"artifact_id": str(artifact.id),
            "component_snapshot": {key: getattr(artifact, "package_name" if key == "package" else key)
                                   for key in ("package", "version", "entry", "sha256")}}},
        scheme_version=artifact.scheme_version)
    run = root / "runs" / str(strategy.id)
    environment = run / "environment"
    environment.mkdir(parents=True)
    copied = run / "wheels" / artifact.package_name / artifact.filename
    copied.parent.mkdir(parents=True)
    shutil.copyfile(artifact_path(artifact), copied)
    component = {"package": artifact.package_name, "version": artifact.version, "entry": artifact.entry,
        "sha256": artifact.sha256, "wheel": str(copied)}
    (environment / "uv.lock").write_text(tomlkit.dumps({"version": 1, "package": [
        {"name": "scheme", "version": artifact.scheme_version,
         "source": {"git": "https://example.invalid/scheme#" + "b" * 40}},
        {"name": artifact.package_name, "version": artifact.version, "source": {"path": str(copied)},
         "wheels": [{"filename": copied.name, "hash": "sha256:" + artifact.sha256}]}]}), encoding="utf-8")
    (environment / "pyproject.toml").write_text(tomlkit.dumps({"project": {"name": "strategy-environment", "version": "0.0.0"}}))
    (run / "input.json").write_text(json.dumps({"kind": "strategy", "algos": {artifact.kind: component},
        "environment": {"lockfile": str(environment / "uv.lock")}, "backtest": {}, "output": str(run / "report")}))
    strategy.accepted_artifacts = {artifact.package_name: artifact_snapshot(artifact)}
    strategy.input_sha256 = hashlib.sha256((run / "input.json").read_bytes()).hexdigest()
    strategy.lock_sha256 = hashlib.sha256((environment / "uv.lock").read_bytes()).hexdigest()
    (run / "report").mkdir()
    (run / "report/run.json").write_text(json.dumps({"status": "success", "input_sha256": strategy.input_sha256,
        "lock_sha256": strategy.lock_sha256, "versions": {"scheme": artifact.scheme_version}}))
    session.add(strategy)
    session.commit()
    return strategy, run


@pytest.mark.parametrize("status", ["queued", "running", "success", "failed", "building"])
def test_fully_accepted_strategy_own_files_allow_deletion_and_actual_worker_admission(registry, status):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    strategy, run = accepted_strategy(session, root, artifact, status=status)
    before_files, before_snapshots = bytes_in(run), deepcopy(strategy.accepted_artifacts)
    policy.check_task(session, strategy, directory=run, kind="strategy")

    response = client.delete(delete_url(artifact))

    assert response.status_code == 204, response.text
    with Session(session.bind) as saved:
        assert saved.get(ResearchArtifact, artifact.id) is None
        saved_strategy = saved.get(Strategy, strategy.id)
        assert saved_strategy.accepted_artifacts == before_snapshots
        assert saved_strategy.components[artifact.kind]["artifact_id"] == str(artifact.id)
        policy.check_task(saved, saved_strategy, directory=run, kind="strategy",
            input_sha256=saved_strategy.input_sha256, lock_sha256=saved_strategy.lock_sha256,
            require_accepted=status in {"queued", "running"})
    assert bytes_in(run) == before_files
    assert policy.accepted_runtime_artifacts(strategy, run, "strategy")[0]["sha256"] == artifact.sha256


@pytest.mark.parametrize("reference", ["direct", "accepted_artifacts", "dependencies"])
def test_unaccepted_building_strategy_nested_selection_blocks_capture_failure(registry, reference):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    item = {"artifact_id": str(artifact.id)} if reference == "direct" else {
        "artifact_id": None, reference: {artifact.package_name: artifact_snapshot(artifact)}}
    strategy = Strategy(name="still capturing", forms={}, components={artifact.kind: item}, status="building")
    session.add(strategy)
    session.commit()
    response = client.delete(delete_url(artifact))
    assert_in_use(response, "strategy_build")
    assert artifact_path(artifact).is_file()
    assert strategy.accepted_artifacts == {}


def test_terminal_descriptor_without_acceptance_is_trace_not_live_lease(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    strategy = Strategy(name="old failed build", forms={}, status="failed",
                        components={artifact.kind: {"artifact_id": str(artifact.id)}})
    session.add(strategy)
    session.commit()
    assert client.delete(delete_url(artifact)).status_code == 204
    assert strategy.components[artifact.kind]["artifact_id"] == str(artifact.id)


def test_unknown_and_unpublished_are_not_deleted(registry):
    session, root, client = registry
    unknown = uuid4()
    response = client.delete(f"/api/v1/artifacts/{unknown}")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "artifact_not_found"
    project = make_project(session, root)
    version, _, _ = make_run(session, root, project)
    artifact = require_artifact(session, version.accepted_artifacts[project.package_name])
    old_bytes = bytes_in(artifact_path(artifact).parent)
    response = client.delete(delete_url(artifact))
    assert response.status_code == 422 and response.json()["detail"]["code"] == "artifact_not_published"
    assert require_artifact(session, artifact.id)
    assert bytes_in(artifact_path(artifact).parent) == old_bytes


def test_filesystem_stage_failure_preserves_registry_row_and_all_bytes(registry, monkeypatch):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    source = artifact_path(artifact).parent
    before = bytes_in(source)
    original = Path.rename
    def rename(path, target):
        if path == source:
            raise PermissionError("cannot stage")
        return original(path, target)
    monkeypatch.setattr(Path, "rename", rename)

    response = client.delete(delete_url(artifact))

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "artifact_delete_failed"
    assert require_artifact(session, artifact.id)
    assert bytes_in(source) == before and not own_stage(root, artifact).exists()


@pytest.mark.parametrize("commit_loss", ["before", "after", "unknown"])
def test_commit_failure_checks_persistent_truth_before_filesystem_restore(registry, monkeypatch, commit_loss):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    identifier = artifact.id
    own = artifact_path(artifact).parent
    before = bytes_in(own)
    original_commit = session.commit
    original_execute = session.execute
    def commit():
        if commit_loss != "before":
            original_commit()
        raise RuntimeError("commit acknowledgement lost")
    monkeypatch.setattr(session, "commit", commit)
    if commit_loss == "unknown":
        # Allow liveness queries; fail only the post-commit truth read.
        def unavailable_after_commit(*args, **kwargs):
            if not own.exists():
                # Stage movement happens before commit: use ORM transaction state
                # to fail only the scalar persisted-truth read on the new transaction.
                statement = str(args[0])
                if "research_artifacts.id, research_artifacts.sha256" in statement:
                    raise RuntimeError("database truth unavailable")
            return original_execute(*args, **kwargs)
        monkeypatch.setattr(session, "execute", unavailable_after_commit)

    response = client.delete(delete_url(artifact))

    assert response.status_code == 503, response.text
    with Session(session.bind) as saved:
        still_exists = saved.get(ResearchArtifact, identifier) is not None
    if commit_loss == "before":
        assert still_exists and bytes_in(own) == before
        assert not own_stage(root, artifact).exists()
        assert response.json()["detail"]["code"] == "artifact_delete_failed"
    else:
        assert not still_exists and not own.exists()
        assert own_stage(root, artifact).is_dir()
        detail = response.json()["detail"]
        if commit_loss == "after":
            assert detail["code"] == "artifact_cleanup_incomplete" and detail["deleted"] is True
        else:
            assert detail["code"] == "artifact_delete_state_unknown" and "deleted" not in detail
    monkeypatch.setattr(session, "commit", original_commit)
    monkeypatch.setattr(session, "execute", original_execute)
    if commit_loss != "before":
        assert client.delete(delete_url(artifact)).status_code == 204
        assert not own_stage(root, artifact).exists()


@pytest.mark.parametrize("partial", [False, True])
def test_cleanup_retry_old_uuid_does_not_touch_re_registered_same_sha_new_uuid(registry, monkeypatch, partial):
    session, root, client = registry
    project, version, run, component, artifact = published(session, root)
    old_identifier = artifact.id
    staged_payload = own_stage(root, artifact) / "artifact"
    original = deletion.shutil.rmtree
    def fail(path, *args, **kwargs):
        if Path(path) == staged_payload:
            if partial:
                (staged_payload / artifact.filename).unlink(missing_ok=True)
                report = staged_payload / "bundle/original/input.json"
                report.unlink(missing_ok=True)
            raise PermissionError("partial cleanup failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(deletion.shutil, "rmtree", fail)

    response = client.delete(delete_url(artifact))

    assert response.status_code == 503, response.text
    assert response.json()["detail"]["deleted"] is True
    assert response.json()["detail"]["code"] == "artifact_cleanup_incomplete"
    with Session(session.bind) as saved:
        assert saved.get(ResearchArtifact, old_identifier) is None
    replacement = register_file(session, Path(component["wheel"]), project,
        component={"entry": component["entry"]}, scheme_version=version.scheme_version)
    session.commit()
    assert replacement.id != old_identifier and replacement.sha256 == artifact.sha256
    replacement_bytes = bytes_in(artifact_path(replacement).parent)
    snapshot_before = deepcopy(version.accepted_artifacts)
    monkeypatch.setattr(deletion.shutil, "rmtree", original)

    assert client.delete(f"/api/v1/artifacts/{old_identifier}").status_code == 204
    assert not own_stage(root, artifact).exists()
    assert bytes_in(artifact_path(replacement).parent) == replacement_bytes
    assert require_artifact(session, replacement.id)
    assert version.accepted_artifacts == snapshot_before
    assert client.delete(f"/api/v1/artifacts/{old_identifier}").status_code == 404
    assert Path(component["wheel"]).is_file() and (run / "report/run.json").is_file()


def test_original_row_recovery_restores_staged_files_then_honors_new_live_reference(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    before = bytes_in(artifact_path(artifact).parent)
    stage = deletion.stage_artifact(session, root / "artifacts", artifact)
    session.rollback()  # Simulate process death before DELETE commit.
    assert stage.is_dir() and not artifact_path(artifact).parent.exists()
    consumer = make_project(session, root)
    consumer.artifact_dependencies = {artifact.package_name: artifact_snapshot(artifact)}
    session.commit()

    response = client.delete(delete_url(artifact))

    assert_in_use(response, "project")
    assert not stage.exists()
    assert bytes_in(artifact_path(artifact).parent) == before
    assert require_artifact(session, artifact.id)


@pytest.mark.parametrize("tamper", ["marker_id", "marker_sha", "payload", "unknown_file", "renamed_uuid", "stage_link"])
def test_cleanup_stage_tampering_is_rejected_without_touching_foreign_files(registry, monkeypatch, tamper):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    stage = own_stage(root, artifact)
    original = deletion.shutil.rmtree
    def fail(path, *args, **kwargs):
        if Path(path) == stage / "artifact":
            raise PermissionError("keep retry stage")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(deletion.shutil, "rmtree", fail)
    assert client.delete(delete_url(artifact)).status_code == 503
    monkeypatch.setattr(deletion.shutil, "rmtree", original)
    target = root / "foreign"
    target.mkdir()
    sentinel = target / "do-not-delete"
    sentinel.write_bytes(b"foreign source run")
    request_id = artifact.id
    if tamper.startswith("marker_"):
        path = stage / "metadata.json"
        marker = json.loads(path.read_text())
        marker["artifact_id" if tamper == "marker_id" else "sha256"] = str(uuid4()) if tamper == "marker_id" else "0" * 64
        path.write_text(json.dumps(marker))
    elif tamper == "payload":
        (stage / "artifact" / artifact.filename).write_bytes(b"changed wheel")
    elif tamper == "unknown_file":
        (stage / "unknown.json").write_bytes(b"unowned")
    elif tamper == "renamed_uuid":
        request_id = uuid4()
        stage.rename(root / "artifacts" / f".delete-{request_id}-{artifact.sha256}")
    else:
        hidden = root / "kept-stage"
        stage.rename(hidden)
        try:
            stage.symlink_to(target, target_is_directory=True)
        except OSError:
            pytest.skip("host does not permit symlink creation")

    response = client.delete(f"/api/v1/artifacts/{request_id}")

    assert response.status_code in {409, 503}, response.text
    assert sentinel.read_bytes() == b"foreign source run"
    with Session(session.bind) as saved:
        assert saved.get(ResearchArtifact, artifact.id) is None
    if tamper == "stage_link":
        stage.unlink()


@pytest.mark.parametrize("link_at", ["sha_root", "registry_root", "shared_root", "shared_ancestor"])
def test_root_or_ancestor_link_is_rejected_without_deleting_any_bytes(registry, monkeypatch, link_at):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    own = artifact_path(artifact).parent
    original_bytes = bytes_in(own)
    if link_at == "sha_root":
        path = own
        destination = root / "kept-sha"
    elif link_at == "registry_root":
        path = root / "artifacts"
        destination = root / "kept-registry"
    elif link_at == "shared_root":
        path = root / "linked-shared"
        destination = root
        monkeypatch.setattr(SoloSettings, "SHARED_DIR", path)
    else:
        destination = root / "ancestor-target"
        shutil.copytree(root / "artifacts", destination / "shared/artifacts")
        path = root / "linked-ancestor"
        monkeypatch.setattr(SoloSettings, "SHARED_DIR", path / "shared")
    if link_at in {"sha_root", "registry_root"}:
        path.rename(destination)
    try:
        path.symlink_to(destination, target_is_directory=True)
    except OSError:
        pytest.skip("host does not permit symlink creation")
    try:
        response = client.delete(delete_url(artifact))
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "artifact_delete_unsafe"
        with Session(session.bind) as saved:
            assert saved.get(ResearchArtifact, artifact.id) is not None
        if link_at == "sha_root":
            assert bytes_in(destination) == original_bytes
        elif link_at == "registry_root":
            assert bytes_in(destination / artifact.sha256) == original_bytes
        else:
            assert bytes_in(own) == original_bytes
    finally:
        path.unlink()
        if link_at in {"sha_root", "registry_root"}:
            destination.rename(path)


def test_normal_venv_python_symlink_is_unlinked_without_touching_external_target(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    target = root / "external-python"
    target.write_bytes(b"external runtime interpreter")
    link = artifact_path(artifact).parent / "bundle/environment/.venv/bin/python"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("host does not permit symlink creation")

    response = client.delete(delete_url(artifact))

    assert response.status_code == 204, response.text
    assert target.read_bytes() == b"external runtime interpreter"
    assert not artifact_path(artifact).parent.exists()


def test_missing_whole_registry_directory_can_remove_only_selected_row(registry):
    session, root, client = registry
    _, _, run, _, artifact = published(session, root)
    shutil.rmtree(artifact_path(artifact).parent)
    assert client.delete(delete_url(artifact)).status_code == 204
    with Session(session.bind) as saved:
        assert saved.get(ResearchArtifact, artifact.id) is None
    assert (run / "report/run.json").is_file()


def test_invalid_own_stage_name_and_other_uuid_stage_are_never_adopted(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    before = bytes_in(artifact_path(artifact).parent)
    invalid = root / "artifacts" / f".delete-{artifact.id}-not-a-digest"
    invalid.mkdir()
    (invalid / "foreign").write_bytes(b"preserve")
    assert client.delete(delete_url(artifact)).status_code == 409
    assert bytes_in(artifact_path(artifact).parent) == before
    invalid.rename(root / "artifacts" / f".delete-{uuid4()}-not-a-digest")
    assert client.delete(delete_url(artifact)).status_code == 204
    assert session.scalar(select(ResearchArtifact.id).where(ResearchArtifact.id == artifact.id)) is None


def test_published_root_with_nonempty_closure_deletes_only_own_directory(registry):
    session, root, client = registry
    _, _, _, _, upstream = published(session, root, project=make_project(session, root, kind="factor"))
    _, version, run, _, artifact = published(session, root,
        assets={upstream.package_name: artifact_path(upstream)})
    assert artifact.dependencies[upstream.package_name]["id"] == str(upstream.id)
    upstream_bytes = bytes_in(artifact_path(upstream).parent)
    before_snapshots = deepcopy(version.accepted_artifacts)
    response = client.delete(delete_url(artifact))
    assert response.status_code == 204, response.text
    assert not artifact_path(artifact).parent.exists()
    assert bytes_in(artifact_path(upstream).parent) == upstream_bytes
    assert require_artifact(session, upstream.id)
    assert version.accepted_artifacts == before_snapshots
    assert (run / "report/run.json").is_file()


def test_nonexpired_archived_consumer_receipt_is_not_an_active_installation(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    consumer = make_project(session, root)
    consumer.archived = True
    installation = ArtifactInstallation(id=uuid4(), consumer_project_id=consumer.id,
        artifacts={"format": 1, "before": {}, "pending": {str(artifact.id): artifact_snapshot(artifact)}, "accepted": None},
        expires_at=datetime.now(UTC) + timedelta(minutes=5))
    session.add(installation)
    session.commit()
    assert client.delete(delete_url(artifact)).status_code == 204
    assert session.get(ArtifactInstallation, installation.id) is not None


def test_marker_only_before_rename_crash_is_recovered_without_overwriting_source(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    source = artifact_path(artifact).parent
    before = bytes_in(source)
    stage = deletion.stage_artifact(session, root / "artifacts", artifact)
    (stage / "artifact").rename(source)  # Reproduce marker-durable/rename-not-done state.
    session.rollback()
    consumer = make_project(session, root)
    consumer.artifact_dependencies = {artifact.package_name: artifact_snapshot(artifact)}
    session.commit()
    response = client.delete(delete_url(artifact))
    assert_in_use(response, "project")
    assert not stage.exists() and bytes_in(source) == before
    assert require_artifact(session, artifact.id)


@pytest.mark.parametrize("failure", ["marker_unlink", "stage_rmdir"])
def test_post_commit_final_marker_cleanup_failure_remains_retryable(registry, monkeypatch, failure):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    stage = own_stage(root, artifact)
    original_unlink, original_rmdir = Path.unlink, Path.rmdir
    def unlink(path, *args, **kwargs):
        if failure == "marker_unlink" and path == stage / "metadata.json":
            raise PermissionError("marker temporarily busy")
        return original_unlink(path, *args, **kwargs)
    def rmdir(path, *args, **kwargs):
        if failure == "stage_rmdir" and path == stage:
            raise PermissionError("stage temporarily busy")
        return original_rmdir(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    response = client.delete(delete_url(artifact))
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["deleted"] is True
    assert (stage / "metadata.json").is_file() and not (stage / "artifact").exists()
    monkeypatch.setattr(Path, "unlink", original_unlink)
    monkeypatch.setattr(Path, "rmdir", original_rmdir)
    assert client.delete(delete_url(artifact)).status_code == 204
    assert not stage.exists()


@pytest.mark.parametrize("row_exists", [False, True])
def test_truly_empty_original_uuid_stage_crash_is_recovered_without_adopting_files(registry, row_exists):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    stage = own_stage(root, artifact)
    if not row_exists:
        assert client.delete(delete_url(artifact)).status_code == 204
    stage.mkdir()
    if row_exists:
        consumer = make_project(session, root)
        consumer.artifact_dependencies = {artifact.package_name: artifact_snapshot(artifact)}
        session.commit()
    response = client.delete(delete_url(artifact))
    if row_exists:
        assert_in_use(response, "project")
        assert artifact_path(artifact).is_file()
    else:
        assert response.status_code == 204, response.text
    assert not stage.exists()


def test_nonempty_stage_without_marker_is_never_adopted(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    stage = own_stage(root, artifact)
    stage.mkdir()
    (stage / "unowned").write_bytes(b"not a valid cleanup stage")
    response = client.delete(delete_url(artifact))
    assert response.status_code == 409, response.text
    assert (stage / "unowned").read_bytes() == b"not a valid cleanup stage"
    assert artifact_path(artifact).is_file()


def test_reentrant_original_uuid_deletion_is_rejected(registry):
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    with deletion._deletion_guard(artifact.id, session):
        response = client.delete(delete_url(artifact))
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "artifact_deletion_in_progress"
    assert artifact_path(artifact).is_file()
