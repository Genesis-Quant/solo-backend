"""The HTTP publication boundary resolves real commit outcomes without changing bytes."""

import hashlib
from datetime import UTC

import pytest
from sqlalchemy.exc import OperationalError

from core.apps.artifacts.models import ResearchArtifact
from core.apps.artifacts.service import artifact_path, verify_bundle
from core.apps.projects import versions
from test_artifacts import make_project, make_run, registry


@pytest.mark.parametrize("outcome", ["rejected", "ack_lost"])
def test_publication_api_retries_real_rollback_and_returns_committed_truth(registry, monkeypatch, outcome):
    session, root, client = registry
    project = make_project(session, root)
    version, run, _ = make_run(session, root, project)
    expected = version.accepted_artifacts[project.package_name]
    originals = {(run / "input.json"): (run / "input.json").read_bytes(),
                 (run / "environment/uv.lock"): (run / "environment/uv.lock").read_bytes()}
    original_read = versions.read_version
    def read_version(record):
        # SQLite strips timezone metadata after rollback expires the real row.
        for name in ("created_at", "finished_at"):
            value = getattr(record, name)
            if value is not None and value.tzinfo is None:
                setattr(record, name, value.replace(tzinfo=UTC))
        return original_read(record)
    monkeypatch.setattr(versions, "read_version", read_version)
    original_commit = session.commit
    injected = False
    def commit():
        nonlocal injected
        if injected:
            return original_commit()
        injected = True
        if outcome == "ack_lost":
            original_commit()
        raise OperationalError("COMMIT", {}, RuntimeError("isolated publication commit fault"))
    monkeypatch.setattr(session, "commit", commit)
    endpoint = f"/api/v1/projects/{project.id}/versions/{version.id}/publish"
    response = client.post(endpoint, json={})
    assert injected
    if outcome == "rejected":
        assert response.status_code == 503, response.text
        assert response.json()["detail"]["code"] == "artifact_transaction_failed"
        from uuid import UUID
        artifact = session.get(ResearchArtifact, UUID(expected["id"]))
        assert artifact.published_at is None and artifact.bundle_manifest_sha256 is None
        pending_bundle = artifact_path(artifact).parent / "bundle"
        before = {path.relative_to(pending_bundle): path.read_bytes() for path in pending_bundle.rglob("*") if path.is_file()}
        response = client.post(endpoint, json={})
        assert response.status_code == 200, response.text
        assert {path.relative_to(pending_bundle): path.read_bytes() for path in pending_bundle.rglob("*") if path.is_file()} == before
    else:
        assert response.status_code == 200, response.text
    assert response.json()["artifactId"] == expected["id"]
    assert response.json()["publishStatus"] == "published"
    from uuid import UUID
    artifact = session.get(ResearchArtifact, UUID(expected["id"]))
    bundle, _ = verify_bundle(artifact)
    seal = hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest()
    assert seal == artifact.bundle_manifest_sha256
    assert all(path.read_bytes() == content for path, content in originals.items())
    assert client.post(endpoint, json={}).status_code == 200
    assert hashlib.sha256((bundle / "manifest.json").read_bytes()).hexdigest() == seal
