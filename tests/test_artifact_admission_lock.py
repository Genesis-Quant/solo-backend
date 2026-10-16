"""Registry mutations keep the existing database deletion lock."""

from pathlib import Path
from uuid import UUID

from core.apps.artifacts import service
from core.apps.artifacts.views import download_wheel
from core.apps.artifacts.models import ResearchArtifact
from test_artifacts import make_project, make_run
from test_artifacts import registry as registry


def test_download_checks_registry_lock_before_resolving_artifact(registry, monkeypatch):
    session, root, _ = registry
    project = make_project(session, root)
    version, _, component = make_run(session, root, project)
    identifier = version.accepted_artifacts[project.package_name]["id"]
    from core.apps.artifacts import views
    calls = []
    required = views.require_artifact
    def lock(current):
        assert current is session
        calls.append("locked")
    def require(*args):
        assert calls == ["locked"]
        return required(*args)
    monkeypatch.setattr(views, "lock_registry", lock)
    monkeypatch.setattr(views, "require_artifact", require)
    response = download_wheel(identifier, session)
    artifact = session.get(ResearchArtifact, UUID(identifier))
    assert Path(response.path) == service.artifact_path(artifact).resolve()
    assert Path(component["wheel"]).is_file() and Path(response.path).is_file()


def test_exact_acceptance_locks_registry_and_returns_uuid_snapshots(registry, monkeypatch):
    session, root, _ = registry
    project = make_project(session, root)
    version, _, _ = make_run(session, root, project)
    identifier = version.accepted_artifacts[project.package_name]["id"]
    calls = []
    monkeypatch.setattr(service, "lock_registry", lambda current: calls.append(current))
    snapshots = service.exact_artifacts(session, [identifier])
    assert calls == [session]
    assert snapshots[project.package_name]["id"] == identifier
