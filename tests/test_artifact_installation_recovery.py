"""Accepted install compensation uses real SQLite, exact locks and owned wheels."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import tomlkit

from core.apps import version_policy as policy
from core.apps.artifacts.models import ArtifactInstallation, ResearchArtifact
from core.apps.artifacts.service import artifact_path, artifact_snapshot, gc_unreferenced, register_file
from test_artifacts import make_project, make_wheel, registry


def accepted_upgrade(registry, *, previous=True):
    session, root, client = registry
    source = make_project(session, root, kind="factor")
    consumer = make_project(session, root)
    directory = policy.project_path(consumer)
    scheme = {"name": "scheme", "version": "1.2.0"}
    old = None
    old_lock = {"package": [scheme]}
    if previous:
        wheel = make_wheel(directory / ".solo-wheels/old.whl", source.package_name, "1.2.1", contents="VALUE = 1")
        old = register_file(session, wheel, source, scheme_version="1.2.0")
        consumer.artifact_dependencies = {old.package_name: artifact_snapshot(old)}
        old_lock["package"].append({"name": old.package_name, "version": old.version, "source": {"path": str(wheel)}})
    old_bytes = tomlkit.dumps(old_lock).encode()
    (directory / "uv.lock").write_bytes(old_bytes)
    wheel = make_wheel(directory / ".solo-wheels/new.whl", source.package_name, "1.2.2", contents="VALUE = 2")
    new = register_file(session, wheel, source, scheme_version="1.2.0")
    session.commit()
    url = f"/api/v1/projects/{consumer.id}/dependencies"
    nonce = str(uuid4())
    response = client.post(url + "/installations", json={"install_id": nonce, "artifacts": [str(new.id)]})
    assert response.status_code == 200, response.text
    new_bytes = tomlkit.dumps({"package": [scheme, {
        "name": new.package_name, "version": new.version, "source": {"path": str(wheel)},
    }]}).encode()
    (directory / "uv.lock").write_bytes(new_bytes)
    response = client.post(url, json={"install_id": nonce, "artifacts": [str(new.id)]})
    assert response.status_code == 200, response.text
    installation = session.get(ArtifactInstallation, UUID(nonce))
    installation.expires_at = datetime.now(UTC) - timedelta(days=1)
    session.commit()
    return consumer, directory, old, new, installation, old_bytes, new_bytes, url


@pytest.mark.parametrize("previous", [False, True])
def test_lost_accept_and_unavailable_abort_keep_before_until_original_nonce_replays(registry, previous):
    session, root, client = registry
    consumer, directory, old, new, installation, old_bytes, new_bytes, url = accepted_upgrade(registry, previous=previous)
    receipt = deepcopy(installation.artifacts)
    # The accept committed, its reply was lost, and filesystem rollback finished.
    # No abort request reached the backend before expiry/GC.
    (directory / "uv.lock").write_bytes(old_bytes)
    assert gc_unreferenced(session)["removed"] == 0
    session.commit()
    session.expire_all()
    assert session.get(ArtifactInstallation, installation.id).artifacts == receipt
    assert consumer.artifact_dependencies == {new.package_name: artifact_snapshot(new)}
    if old is not None:
        assert session.get(ResearchArtifact, old.id) is not None
        assert artifact_path(old).is_file()
    assert (directory / "uv.lock").read_bytes() == old_bytes

    aborted = client.post(url + f"/installations/{installation.id}/abort", json={})
    assert aborted.status_code == 200, aborted.text
    assert session.get(ArtifactInstallation, installation.id) is None
    assert consumer.artifact_dependencies == receipt["before"]
    assert (directory / "uv.lock").read_bytes() == old_bytes
    assert client.post(url + f"/installations/{installation.id}/abort", json={}).status_code == 200
    assert gc_unreferenced(session)["removed"] == 1
    session.commit()
    if old is not None:
        assert artifact_path(old).is_file()


def test_expired_accepted_receipt_blocks_new_dependency_mutations(registry):
    session, root, client = registry
    consumer, directory, old, new, installation, old_bytes, new_bytes, url = accepted_upgrade(registry)
    before = deepcopy(consumer.artifact_dependencies)
    for body in ({"install_id": str(uuid4())}, {"install_id": str(uuid4()), "artifacts": [str(new.id)]}):
        response = client.post(url + "/installations", json=body)
        assert response.status_code == 409, response.text
    response = client.post(url, json={"artifacts": [str(new.id)]})
    assert response.status_code == 409, response.text
    assert consumer.artifact_dependencies == before
    assert session.get(ArtifactInstallation, installation.id) is not None
    assert (directory / "uv.lock").read_bytes() == new_bytes
    assert client.post(url + f"/installations/{installation.id}/complete", json={}).status_code == 200
    assert client.post(url + "/installations", json={"install_id": str(uuid4())}).status_code == 200


@pytest.mark.parametrize("outcome", ["complete", "abort"])
def test_finalization_verifies_exact_filesystem_outcome_before_releasing_receipt(registry, outcome):
    session, root, client = registry
    consumer, directory, old, new, installation, old_bytes, new_bytes, url = accepted_upgrade(registry)
    intended = new_bytes if outcome == "complete" else old_bytes
    wrong = old_bytes if outcome == "complete" else new_bytes
    (directory / "uv.lock").write_bytes(wrong)
    response = client.post(url + f"/installations/{installation.id}/{outcome}", json={})
    assert response.status_code == 422, response.text
    assert session.get(ArtifactInstallation, installation.id) is not None
    assert consumer.artifact_dependencies == {new.package_name: artifact_snapshot(new)}
    assert gc_unreferenced(session)["removed"] == 0
    session.commit()
    (directory / "uv.lock").write_bytes(intended)
    response = client.post(url + f"/installations/{installation.id}/{outcome}", json={})
    assert response.status_code == 200, response.text
    assert session.get(ArtifactInstallation, installation.id) is None
    assert (directory / "uv.lock").read_bytes() == intended


@pytest.mark.parametrize("reference", ["before", "pending", "accepted"])
def test_expired_accepted_install_protects_published_artifact_delete(registry, reference):
    from test_artifact_deletion import assert_in_use, published
    session, root, client = registry
    _, _, _, _, artifact = published(session, root)
    consumer = make_project(session, root)
    snapshot = artifact_snapshot(artifact)
    receipt = {"format": 1, "before": {}, "pending": {}, "accepted": {}}
    receipt[reference] = {artifact.package_name: snapshot}
    installation = ArtifactInstallation(id=uuid4(), consumer_project_id=consumer.id,
        artifacts=receipt, expires_at=datetime.now(UTC) - timedelta(days=1))
    session.add(installation)
    session.commit()
    response = client.delete(f"/api/v1/artifacts/{artifact.id}")
    assert_in_use(response, "installation")
    assert session.get(ArtifactInstallation, installation.id) is not None
    assert artifact_path(artifact).is_file()
