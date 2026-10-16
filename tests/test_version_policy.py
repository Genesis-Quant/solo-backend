"""Task admission uses submitted database identity and unchanged release policy."""

import json
import os
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from core.apps import version_policy as policy
from core.apps.projects import deletion
from core.apps.projects.models import ProjectVersion
from core.apps.strategies.models import Strategy
from test_artifacts import make_project, make_run
from test_artifacts import registry as registry


@pytest.mark.parametrize("kind", ["factor", "model", "optimize", "control", "execution", "strategy"])
def test_task_check_requires_only_kind_and_never_reads_task_files(registry, monkeypatch, kind):
    session, root, client = registry
    if kind == "strategy":
        record = Strategy(name="submitted", forms={}, components={}, status="queued", scheme_version="1.2.0")
    else:
        project = make_project(session, root, kind=kind)
        record = ProjectVersion(project_id=project.id, number=1, package_version="1.2.1", status="queued", scheme_version="1.2.0")
    session.add(record)
    session.commit()
    monkeypatch.setattr(policy, "_policy_key", None)
    original = Path.read_text
    reads = []
    def read(path, *args, **kwargs):
        reads.append(path)
        assert path == policy.POLICY_FILE
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(policy, "task_files", lambda *args, **kwargs: pytest.fail("task files must not be read"))
    response = client.post(f"/api/v1/version-policy/tasks/{record.id}/check", json={"kind": kind, "ignored": "extra"})
    assert response.status_code == 200, response.text
    assert response.json() == {"allowed": True}
    assert len(reads) == 1
    assert not (root / "runs").exists()


@pytest.mark.parametrize("status", ["building", "submit_failed", "success", "failed"])
def test_task_check_rejects_unsubmitted_or_terminal_status(registry, status):
    session, root, client = registry
    project = make_project(session, root)
    version = ProjectVersion(project_id=project.id, number=1, package_version="1.2.1", status=status, scheme_version="1.2.0")
    session.add(version)
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{version.id}/check", json={"kind": "model"})
    assert response.status_code == 422
    assert set(response.json()["detail"]) == {"code", "reason"}
    assert response.json()["detail"]["code"] == "task_not_submitted"


def test_task_not_found_is_404_and_kind_mismatch_is_422(registry):
    session, root, client = registry
    assert client.post(f"/api/v1/version-policy/tasks/{uuid4()}/check", json={"kind": "model"}).status_code == 404
    project = make_project(session, root, kind="factor")
    version = ProjectVersion(project_id=project.id, number=1, package_version="1.2.1", status="running", scheme_version="1.2.0")
    session.add(version)
    session.commit()
    for kind in ("model", "strategy"):
        response = client.post(f"/api/v1/version-policy/tasks/{version.id}/check", json={"kind": kind})
        assert response.status_code == 422 and response.json()["detail"]["code"] == "task_identity_mismatch"


@pytest.mark.parametrize("source", ["scheme", "template-tag", "template-commit", "upstream-scheme", "upstream-series"])
def test_retired_or_mismatched_accepted_sources_stay_rejected_without_origin_files(registry, source):
    session, root, client = registry
    project = make_project(session, root)
    sources = policy.project_sources(project)
    scheme = "1.2.0"
    upstream_scheme, upstream_version = "1.2.0", "1.2.1"
    if source == "scheme":
        scheme = "1.1.7"
    elif source == "template-tag":
        sources["template"]["tag"] = "v1.0.1"
    elif source == "template-commit":
        sources["template"]["commit"] = policy.load_policy()["retired_releases"]["template"][0]["commit"]
    elif source == "upstream-scheme":
        upstream_scheme = "1.1.7"
    else:
        upstream_scheme, upstream_version = "1.3.0", "1.3.1"
    version = ProjectVersion(project_id=project.id, number=1, package_version="1.2.1", status="queued", scheme_version=scheme,
        accepted_artifacts={project.package_name: {"package": project.package_name, "version": upstream_version, "kind": "model",
            "scheme_version": upstream_scheme, "sources": sources}})
    session.add(version)
    session.commit()
    response = client.post(f"/api/v1/version-policy/tasks/{version.id}/check", json={"kind": "model"})
    expected_code = "task_identity_mismatch" if source == "upstream-series" else "version_retired"
    assert response.status_code == 422 and response.json()["detail"]["code"] == expected_code


@pytest.mark.parametrize("tag,actual,allowed", [
    ("stable-current", "1.2.0", True), ("v1.1.42", "1.2.0", True),
    ("stable-current", None, False), ("v1.2.0", None, True), ("stable-current", "1.1.42", False),
])
def test_template_actual_and_identity_policy_result_is_preserved(registry, tag, actual, allowed):
    session, root, client = registry
    project = make_project(session, root)
    project.template_tag, project.template_package_version = tag, actual
    session.commit()
    response = client.post(f"/api/v1/version-policy/projects/{project.id}/check")
    assert response.status_code == (200 if allowed else 422), response.text


def test_project_check_rejects_lifecycle_guard_before_reading_project_files(registry, monkeypatch):
    session, root, client = registry
    project = make_project(session, root)
    with deletion.project_lifecycle_guard(project.id, session):
        assert deletion.project_lifecycle_busy(project.id)
        assert not deletion.project_lifecycle_busy(uuid4())
        with monkeypatch.context() as patch:
            patch.setattr(policy, "require_project", lambda *args, **kwargs: pytest.fail("busy project files must not be read"))
            response = client.post(f"/api/v1/version-policy/projects/{project.id}/check")
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {
            "code": "project_busy", "reason": "项目正在升级、修改或删除，请稍后重试",
        }
    assert not deletion.project_lifecycle_busy(project.id)
    assert client.post(f"/api/v1/version-policy/projects/{project.id}/check").json() == {"allowed": True}


@pytest.mark.parametrize("kind", ["scheme", "template"])
def test_all_retired_release_tags_and_commits_override_active_version(kind):
    for release in policy.load_policy()["retired_releases"][kind]:
        assert policy.release_reason(kind, tag=release["tag"], version="9.9.9") == release["reason"]
        assert policy.release_reason(kind, commit=release["commit"], version="9.9.9") == release["reason"]


def test_floor_and_grandfather_policy_are_unchanged(tmp_path, monkeypatch):
    assert policy.release_reason("scheme", version="1.1.99")
    assert policy.release_reason("scheme", version="1.3.7") is None
    body = deepcopy(policy.load_policy())
    body["grandfathered_runs"] = [{"run_id": str(uuid4())}]
    file = tmp_path / "policy.json"
    file.write_text(json.dumps(body))
    monkeypatch.setattr(policy, "POLICY_FILE", file)
    with pytest.raises(policy.VersionPolicyError, match="不支持启用 grandfather"):
        policy.load_policy()


def test_policy_cache_reloads_only_when_file_modification_time_changes(tmp_path, monkeypatch):
    body = deepcopy(policy.load_policy())
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    monkeypatch.setattr(policy, "POLICY_FILE", path)
    monkeypatch.setattr(policy, "_policy_key", None)
    monkeypatch.setattr(policy, "_policy_data", None)
    original = Path.read_text
    reads = []
    def read(current, *args, **kwargs):
        reads.append(current)
        return original(current, *args, **kwargs)
    monkeypatch.setattr(Path, "read_text", read)
    first = policy.load_policy()
    assert policy.load_policy() is first
    assert policy.release_reason("scheme", version="1.2.0") is None
    assert reads == [path]
    previous_time = path.stat().st_mtime_ns
    body["minimum_versions"]["scheme"] = "1.3.0"
    path.write_text(json.dumps(body), encoding="utf-8")
    os.utime(path, ns=(previous_time, previous_time + 1_000_000_000))
    assert policy.release_reason("scheme", version="1.2.0")
    assert policy.load_policy() is not first
    assert reads == [path, path]


@pytest.mark.parametrize("scheme,retired", [("1.2.0", False), ("1.1.7", True)])
def test_historical_retirement_keeps_using_recorded_run_actual_when_db_value_missing(registry, scheme, retired):
    session, root, _ = registry
    project = make_project(session, root)
    version, _, _ = make_run(session, root, project, scheme=scheme, accepted=False)
    version.scheme_version = None
    assert bool(policy.version_retirement(version)) is retired


def test_task_check_keeps_snapshots_independent_of_current_project(registry):
    session, root, client = registry
    project = make_project(session, root)
    sources = policy.project_sources(project)
    version = ProjectVersion(project_id=project.id, number=1, package_version="1.2.1", status="queued", scheme_version="1.2.0",
        accepted_artifacts={project.package_name: {"package": project.package_name, "version": "1.2.1", "kind": "model",
            "scheme_version": "1.2.0", "sources": sources}})
    session.add(version)
    project.template_commit = policy.load_policy()["retired_releases"]["template"][0]["commit"]
    session.commit()
    assert client.post(f"/api/v1/version-policy/tasks/{version.id}/check", json={"kind": "model"}).json() == {"allowed": True}
    assert session.scalar(select(ProjectVersion)) is version
