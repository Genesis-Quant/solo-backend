"""Registry admission is serialized with release deletion before references commit."""

from copy import deepcopy
from uuid import UUID

from sqlalchemy import event
from sqlalchemy.orm import Session

from core.apps import version_policy as policy
from core.apps.artifacts import service as artifacts
from core.apps.artifacts.models import ResearchArtifact
from core.apps.strategies import views
from core.apps.strategies.models import Strategy
from core.apps.strategies.service import KINDS
from test_artifacts import make_project, make_run, registry


def test_capture_locks_before_existing_snapshot_and_file_validation(registry, monkeypatch):
    session, root, _ = registry
    project = make_project(session, root)
    version, directory, _ = make_run(session, root, project)
    expected = deepcopy(version.accepted_artifacts)
    calls = []
    task_files = policy.task_files

    def locked(current):
        assert current is session
        calls.append("locked")

    def files(*args, **kwargs):
        assert calls == ["locked"]
        calls.append("files")
        return task_files(*args, **kwargs)

    monkeypatch.setattr(artifacts, "lock_registry", locked)
    monkeypatch.setattr(policy, "task_files", files)
    assert policy.capture_task_artifacts(session, version, project=project,
                                         directory=directory, kind=project.kind) == expected
    assert calls == ["locked", "files"]


def test_idempotent_publication_refreshes_root_after_registry_lock(registry, monkeypatch):
    session, root, _ = registry
    project = make_project(session, root)
    version, _, _ = make_run(session, root, project)
    snapshot = version.accepted_artifacts[project.package_name]
    cached = session.get(ResearchArtifact, UUID(snapshot["id"]))
    assert cached.published_at is None
    with Session(session.get_bind(), expire_on_commit=False) as other:
        published = artifacts.publish_version(other, other.get(type(project), project.id),
                                               other.get(type(version), version.id))
        other.commit()
        published_at, seal = published.published_at, published.bundle_manifest_sha256
    assert cached.published_at is None
    calls = []
    require = artifacts.require_artifact

    def locked(current):
        assert current is session
        calls.append("locked")

    def required(*args, **kwargs):
        assert calls and calls[0] == "locked"
        return require(*args, **kwargs)

    monkeypatch.setattr(artifacts, "lock_registry", locked)
    monkeypatch.setattr(artifacts, "require_artifact", required)
    result = artifacts.publish_version(session, project, version)
    assert result is cached
    assert result.published_at.replace(tzinfo=None) == published_at.replace(tzinfo=None)
    assert result.bundle_manifest_sha256 == seal
    session.commit()


def test_strategy_selection_lock_releases_after_descriptor_commit_before_build(registry, monkeypatch):
    session, root, _ = registry
    project = make_project(session, root)
    version, _, component = make_run(session, root, project)
    snapshot = version.accepted_artifacts[project.package_name]
    locked = False
    calls = []

    def acquire(current):
        nonlocal locked
        assert current is session
        locked = True
        calls.append("locked")

    def committed(current):
        nonlocal locked
        assert current is session
        locked = False
        calls.append("committed")

    def selected(current, selection):
        assert current is session and locked
        assert calls == ["locked"]
        calls.append("selected")
        return {kind: ({"artifact_id": snapshot["id"], "component": component,
                        "accepted_artifacts": {project.package_name: snapshot}}
                       if kind == "model" else {"version_id": None}) for kind in KINDS}

    def build(components, forms, directory):
        assert not locked
        assert calls == ["locked", "selected", "committed"]
        descriptor = session.get(Strategy, UUID(directory.name))
        assert descriptor.components["model"]["artifact_id"] == snapshot["id"]
        assert not descriptor.accepted_artifacts and not descriptor.input_sha256 and not descriptor.lock_sha256
        calls.append("build")
        raise ValueError("isolated build failure")

    monkeypatch.setattr(views, "lock_registry", acquire)
    monkeypatch.setattr(views, "selected", selected)
    monkeypatch.setattr(views, "require_components", lambda components: None)
    monkeypatch.setattr(views, "build", build)
    event.listen(session, "after_commit", committed)
    try:
        response = views.create_strategy(views.CreateStrategy(
            name="serialized admission", selections=views.Selection(artifacts={"model": UUID(snapshot["id"])}),
            forms={kind: {} for kind in KINDS},
        ), session)
    finally:
        event.remove(session, "after_commit", committed)
    assert response["status"] == "failed"
    assert response["error"] == "isolated build failure"
    assert calls == ["locked", "selected", "committed", "build", "committed"]


def test_download_registry_lock_lives_until_file_response_has_sent(registry, monkeypatch):
    from fastapi.responses import FileResponse

    from core.apps.artifacts import views as artifact_views
    from core.database.session import get_database_session
    from main import app

    session, root, client = registry
    project = make_project(session, root)
    version, _, component = make_run(session, root, project)
    identifier = version.accepted_artifacts[project.package_name]["id"]
    events = []
    original = FileResponse.__call__
    previous = app.dependency_overrides[get_database_session]

    def request_session():
        events.append("opened")
        try:
            yield session
        finally:
            events.append("closed")

    def lock(current):
        assert current is session
        events.append("locked")

    async def send(response, scope, receive, emit):
        assert events == ["opened", "locked"]
        events.append("sending")
        await original(response, scope, receive, emit)
        assert "closed" not in events
        events.append("sent")

    monkeypatch.setattr(artifact_views, "lock_registry", lock)
    monkeypatch.setattr(FileResponse, "__call__", send)
    app.dependency_overrides[get_database_session] = request_session
    try:
        response = client.get(f"/api/v1/artifacts/{identifier}/wheel")
    finally:
        app.dependency_overrides[get_database_session] = previous
    assert response.status_code == 200
    from pathlib import Path

    assert response.content == Path(component["wheel"]).read_bytes()
    assert events == ["opened", "locked", "sending", "sent", "closed"]
