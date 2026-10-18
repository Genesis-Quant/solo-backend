"""保存任务中断、调度重试和重复请求的状态回归。"""

import json
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps.artifacts.service import artifact_snapshot, register_file
from core.apps.projects import versions
from core.apps.projects.models import Project, ProjectVersion
from core.scheduler.client import DolphinSchedulerError
from test_artifacts import make_project, make_run, make_wheel
from test_projects import create_cleanup_project, projects_client  # noqa: F401 -- imported pytest fixture


@pytest.fixture
def submission(tmp_path, monkeypatch):
    version = ProjectVersion(
        id=uuid4(), project_id=uuid4(), number=1, package_version="1.2.1",
        status="building", parameters={}, error="", note="",
        created_at=datetime.now(UTC),
    )
    session = MagicMock()
    session.scalar.return_value = version
    session.get.return_value = SimpleNamespace(id=version.project_id, name="submission", kind="factor", archived=False, scheme_version="v1.2.0",
                                               scheme_commit="b" * 40, template_tag="v1.2.0", template_commit="a" * 40, template_package_version="1.2.0", package_name=f"factor-{version.project_id.hex[:4]}")
    session.get.side_effect = lambda model, identifier: None if model is versions.ProjectDeletion else session.get.return_value
    directory = tmp_path / "runs" / str(version.id)
    directory.mkdir(parents=True)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    factory = MagicMock()
    client = factory.return_value.__enter__.return_value
    client.instances.return_value = []
    client.instance.return_value = {"state": "RUNNING_EXECUTION"}
    monkeypatch.setattr(versions, "DolphinSchedulerClient", factory)
    monkeypatch.setattr(versions, "version_directory", lambda _: directory)
    package = f"factor-{version.project_id.hex[:4]}"
    wheel = directory / "factor.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA", f"Name: {package}\nVersion: 1.2.1\nRequires-Dist: scheme>=1.2.0,<1.3.0\n")
        archive.writestr(package.replace("-", "_") + "/__init__.py", "")
    (directory / "uv.lock").write_text(f'[[package]]\nname="scheme"\nversion="1.2.0"\n[[package]]\nname="{package}"\nversion="1.2.1"\nsource={{path="factor.whl"}}\n')
    session.scalars.return_value = [session.get.return_value]
    session.scalar.side_effect = lambda query: None if "project_versions.package_version =" in str(query) else version
    (directory / "input.json").write_text(json.dumps({
        "kind": "factor",
        "factor": {"package": package, "version": "1.2.1", "wheel": str(wheel), "params": {},
                   "entry": f"{package.replace('-', '_')}:Factor"},
        "environment": {"lockfile": str(directory / "uv.lock")}, "analysis": {},
    }), encoding="utf-8")
    (directory / "build.json").write_text('{"scheme_version":"1.2.0"}', encoding="utf-8")

    def capture(database, record, *, project, directory, kind):
        data = json.loads((directory / "input.json").read_text())
        component = data["factor"] if kind == "factor" else data["algos"][kind]
        return {component["package"]: {
            **{key: component[key] for key in ("package", "version", "entry")},
            "kind": kind, "sources": versions.policy.project_sources(project), "scheme_version": "1.2.0",
        }}

    monkeypatch.setattr(versions.policy, "capture_task_artifacts", capture)
    return version, session, factory, client, directory


def submit(version, session):
    return versions.submit_version(version.project_id, version.id, versions.BuildResult(), session)


def test_factor_submission_keeps_closed_dates_only_in_analysis(submission):
    version, session, _, client, directory = submission
    path = directory / "input.json"
    data = json.loads(path.read_text())
    data["factor"]["params"] = {"universe": {"pool": "全市场"}, "window": 20}
    data["analysis"] = {
        "start": "2025-01-06", "end": "2025-01-06", "universe": {"pool": "全市场"},
        "columns": ["score"], "period": 5, "price_column": "open_hfq", "max_lag": 3,
        "groups": 5, "n_select": 10, "weight": "equal",
    }
    path.write_text(json.dumps(data), encoding="utf-8")
    result = submit(version, session)
    assert result["phase"] == "queued"
    assert version.parameters["factor"] == data["factor"]["params"]
    assert version.parameters["analysis"] == data["analysis"]
    assert "start" not in version.parameters["factor"] and "end" not in version.parameters["factor"]
    client.start.assert_called_once_with("factor", str(path))


@pytest.mark.parametrize("scheme_version,series", [("1.2.0", "1.2"), ("1.2.7", "1.2"), ("1.2.99", "1.2")])
@pytest.mark.parametrize("previous_number", [0, 7])
def test_create_version_uses_actual_locked_scheme_series(tmp_path, monkeypatch, scheme_version, series, previous_number):
    project = SimpleNamespace(id=uuid4(), name="create-version", kind="factor", archived=False,
                              scheme_version="v9.9.9", scheme_commit="b" * 40,
                              template_tag="v1.2.0", template_commit="a" * 40, template_package_version="1.2.0")
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    lock = tmp_path / "uv.lock"
    original = f'[[package]]\nname="scheme"\nversion="{scheme_version}"\n'
    lock.write_text(original, encoding="utf-8")
    monkeypatch.setattr(versions, "project_directory", lambda _: tmp_path)
    monkeypatch.setattr(versions, "read_version", lambda version: {"packageVersion": version.package_version})
    session = MagicMock()
    session.scalar.side_effect = [project, None, previous_number]
    session.get.return_value = None
    result = versions.create_version(project.id, versions.CreateVersion(note="保存"), session)
    version = session.add.call_args.args[0]
    assert result["packageVersion"] == f"{series}.{previous_number + 1}"
    assert version.number == previous_number + 1
    assert version.note == "保存"
    assert project.scheme_version == "v9.9.9"
    assert lock.read_text(encoding="utf-8") == original
    session.commit.assert_called_once()


@pytest.mark.parametrize("content", [
    None, "not valid toml", "version = 1", '[[package]]\nname="other"\nversion="1.1.0"\n',
    '[[package]]\nname="scheme"\nversion="invalid"\n', '[[package]]\nname="scheme"\n',
    '[[package]]\nname="scheme"\nversion="1.1.0"\n[[package]]\nname="scheme"\nversion="1.1.7"\n',
    *[f'[[package]]\nname="scheme"\nversion="{value}"\n' for value in (
        "1.1.7rc1", "1.1.7.dev1", "1.1.7.post1", "1.1.7+local", "1!1.1.7", "1.1.7.0",
    )],
])
def test_create_version_rejects_missing_or_ambiguous_scheme_lock(tmp_path, monkeypatch, content):
    project = SimpleNamespace(id=uuid4(), name="invalid-lock", kind="factor", archived=False,
                              scheme_version="v1.2.0", scheme_commit="b" * 40,
                              template_tag="v1.2.0", template_commit="a" * 40, template_package_version="1.2.0")
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    if content is not None:
        (tmp_path / "uv.lock").write_text(content, encoding="utf-8")
    monkeypatch.setattr(versions, "project_directory", lambda _: tmp_path)
    session = MagicMock()
    session.scalar.side_effect = [project, None, None]
    session.get.return_value = None
    with pytest.raises(HTTPException) as error:
        versions.create_version(project.id, versions.CreateVersion(), session)
    assert error.value.status_code == 422
    session.add.assert_not_called()
    session.commit.assert_not_called()


def test_cancel_orphan_build_ignores_late_completion(submission):
    version, session, factory, _, _ = submission
    result = versions.cancel_build(version.project_id, version.id, session)
    assert result["phase"] == "failed"
    assert version.finished_at is not None
    assert "取消" in version.error
    session.commit.assert_called_once()
    assert submit(version, session)["phase"] == "failed"
    factory.assert_not_called()


@pytest.mark.parametrize("status", ["building", "submit_failed"])
@pytest.mark.parametrize("source", ["scheme", "template", "unknown"])
def test_failed_build_callback_records_failure_without_release_admission(submission, status, source):
    version, session, factory, _, directory = submission
    version.status = status
    project = session.get.return_value
    if source == "scheme":
        project.scheme_version = "v1.1.0"
    elif source == "template":
        project.template_tag = "v1.0.1"
    else:
        project.template_tag = "custom-unknown"
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    result = versions.submit_version(
        version.project_id, version.id, versions.BuildResult(error="后台准入检查失败，未执行构建"), session,
    )
    assert result["phase"] == "failed"
    assert result["error"] == "后台准入检查失败，未执行构建"
    assert version.finished_at is not None
    session.commit.assert_called_once()
    factory.assert_not_called()
    assert {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()} == before


@pytest.mark.parametrize("source", ["scheme", "template"])
def test_successful_build_callback_still_rejects_retired_execution(submission, source):
    version, session, factory, _, _ = submission
    project = session.get.return_value
    if source == "scheme":
        project.scheme_version = "v1.1.0"
    else:
        project.template_tag = "v1.0.1"
    with pytest.raises(HTTPException) as error:
        submit(version, session)
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "version_retired"
    assert version.status == "failed" and version.error == error.value.detail["reason"]
    assert version.finished_at is not None
    session.commit.assert_called_once()
    factory.assert_not_called()
    assert submit(version, session)["phase"] == "failed"
    session.commit.assert_called_once()


def test_retry_submission_rechecks_retired_frozen_scheme_without_recapturing_artifacts(submission):
    version, session, factory, _, directory = submission
    version.status = "submit_failed"
    version.accepted_artifacts = {"previous": {"package": "factor-abcd"}}
    lockfile = directory / "uv.lock"
    lockfile.write_text('[[package]]\nname="scheme"\nversion="1.2.0"\nsource={git="https://example.invalid/scheme#'
                        + versions.policy.load_policy()["retired_releases"]["scheme"][0]["commit"] + '"}\n')
    with pytest.raises(HTTPException) as error:
        submit(version, session)
    assert error.value.status_code == 422 and error.value.detail["code"] == "version_retired"
    assert version.status == "failed" and version.error == error.value.detail["reason"]
    assert version.finished_at is not None
    assert version.accepted_artifacts == {"previous": {"package": "factor-abcd"}}
    factory.assert_not_called()
    session.commit.assert_called_once()


@pytest.mark.parametrize("failure,reason", [
    ("kind", "研究类型与项目不一致"), ("package-version", "候选包版本或研究类型不一致"),
    ("missing-input", "构建产物不完整"), ("invalid-input", "构建产物不完整"),
    ("missing-wheel", "任务产物不在版本目录中"), ("missing-lock", "任务产物不在版本目录中"),
    ("actual-scheme", "build.json ActualScheme 与冻结锁文件不一致"),
    ("result-series", "候选成果必须使用实际 Scheme 的主、次版本"),
    ("retired-scheme", "已退役"), ("recorded-retired-scheme", "已退役"),
    ("artifact", "injected artifact rejection"),
])
def test_rejected_submission_persists_failure_and_allows_new_version(projects_client, monkeypatch, failure, reason):  # noqa: F811
    client, root, engine = projects_client
    project = create_cleanup_project(client)
    project_id = UUID(project["id"])
    url = f'/api/v1/projects/{project["id"]}/versions'
    response = client.post(url, json={})
    assert response.status_code == 201, response.text
    version_id = UUID(response.json()["id"])
    directory = root / "runs" / str(version_id)
    directory.mkdir(parents=True)
    (directory / "factor.whl").write_bytes(b"unused wheel")
    (directory / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n', encoding="utf-8")
    (directory / "build.json").write_text('{"scheme_version":"1.2.0"}', encoding="utf-8")
    data = {"kind": "factor", "factor": {"package": project["packageName"], "version": "1.2.1",
            "wheel": "factor.whl", "params": {}}, "environment": {"lockfile": "uv.lock"}, "analysis": {}}
    if failure == "kind":
        data["kind"] = "model"
    elif failure == "package-version":
        data["factor"]["version"] = "1.2.2"
    elif failure == "missing-wheel":
        (directory / "factor.whl").unlink()
    elif failure == "missing-lock":
        (directory / "uv.lock").unlink()
    elif failure == "actual-scheme":
        (directory / "build.json").write_text('{"scheme_version":"1.2.7"}', encoding="utf-8")
    elif failure == "result-series":
        (directory / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.3.0"\n', encoding="utf-8")
    elif failure == "retired-scheme":
        (directory / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.1.7"\n', encoding="utf-8")
    elif failure == "recorded-retired-scheme":
        with Session(engine) as session:
            session.get(ProjectVersion, version_id).scheme_version = "1.1.7"
            session.commit()
    elif failure == "artifact":
        def reject(*args, **kwargs):
            raise ValueError("injected artifact rejection")
        monkeypatch.setattr(versions.policy, "capture_task_artifacts", reject)
    if failure != "missing-input":
        (directory / "input.json").write_text("null" if failure == "invalid-input" else json.dumps(data), encoding="utf-8")
    scheduler = MagicMock()
    monkeypatch.setattr(versions, "DolphinSchedulerClient", scheduler)
    response = client.post(f"{url}/{version_id}/submit", json={})
    assert response.status_code == 422, response.text
    detail = response.json()["detail"]
    recorded_reason = detail["reason"] if isinstance(detail, dict) else detail
    with Session(engine) as session:
        version = session.get(ProjectVersion, version_id)
        assert version.status == "failed"
        assert version.error == recorded_reason and reason in version.error
        assert version.finished_at is not None
        assert session.get(Project, project_id).archived is False
    assert client.post(f"{url}/{version_id}/submit", json={}).json()["phase"] == "failed"
    saved = client.post(url, json={})
    assert saved.status_code == 201, saved.text
    assert saved.json()["packageVersion"] == "1.2.2" and saved.json()["phase"] == "building"
    assert saved.json()["id"] != str(version_id)
    scheduler.assert_not_called()


@pytest.mark.parametrize("status", ["failed", "success"])
def test_late_failed_build_callback_preserves_completed_record(submission, status):
    version, session, factory, _, _ = submission
    version.status = status
    version.error = "已完成的状态"
    version.finished_at = datetime.now(UTC)
    finished = version.finished_at
    result = versions.submit_version(
        version.project_id, version.id, versions.BuildResult(error="迟到的失败回调"), session,
    )
    assert result["phase"] == status
    assert version.error == "已完成的状态"
    assert version.finished_at == finished
    session.commit.assert_not_called()
    factory.assert_not_called()


def test_failed_build_callback_rejects_wrong_record_owner(submission):
    version, session, factory, _, _ = submission
    session.scalar.side_effect = None
    session.scalar.return_value = None
    with pytest.raises(HTTPException) as error:
        versions.submit_version(uuid4(), version.id, versions.BuildResult(error="失败回调"), session)
    assert error.value.status_code == 404
    assert version.status == "building"
    session.commit.assert_not_called()
    factory.assert_not_called()


def test_cannot_cancel_already_submitted_build(submission):
    version, session, _, _, _ = submission
    version.status = "queued"
    with pytest.raises(HTTPException) as error:
        versions.cancel_build(version.project_id, version.id, session)
    assert error.value.status_code == 409
    assert version.status == "queued"


def test_login_failure_retries_existing_artifacts(submission):
    version, session, factory, client, directory = submission
    original = (directory / "input.json").read_bytes()
    factory.return_value.__enter__.side_effect = DolphinSchedulerError("login failed")
    assert submit(version, session)["phase"] == "submit_failed"
    client.start.assert_not_called()
    assert versions.read_version(version)["status"] == "failed"
    factory.return_value.__enter__.side_effect = None
    assert submit(version, session)["phase"] == "queued"
    client.start.assert_called_once_with("factor", str(directory / "input.json"))
    assert version.error == ""
    assert version.finished_at is None
    assert (directory / "input.json").read_bytes() == original
    submit(version, session)
    assert client.start.call_count == 1


@pytest.mark.parametrize("retired_source", [None, "scheme-tag", "scheme-commit", "template-tag", "template-commit"])
def test_retry_rechecks_accepted_upstream_without_recapture(projects_client, monkeypatch, retired_source):  # noqa: F811
    client, root, engine = projects_client
    with Session(engine, expire_on_commit=False) as session:
        producer = make_project(session, root, kind="factor")
        producer.scheme_version, producer.scheme_commit = "upstream-scheme", "d" * 40
        producer.template_tag, producer.template_commit = "upstream-template", "c" * 40
        session.commit()
        wheel = make_wheel(root / "local" / "upstream.whl", producer.package_name)
        upstream = register_file(session, wheel, producer, scheme_version="1.2.0")
        consumer = make_project(session, root)
        consumer.artifact_dependencies = {upstream.package_name: artifact_snapshot(upstream)}
        session.commit()
        original_sources = versions.policy.project_sources(consumer)
        original_dependencies = deepcopy(consumer.artifact_dependencies)
        version, directory, _ = make_run(session, root, consumer,
            assets={upstream.package_name: wheel}, accepted=False)
        version.status, version.finished_at = "building", None
        session.commit()
        project_id, version_id, upstream_name = consumer.id, version.id, upstream.package_name
    (directory / "build.json").write_text('{"scheme_version":"1.2.0"}', encoding="utf-8")
    factory = MagicMock()
    scheduler = factory.return_value.__enter__.return_value
    scheduler.start.side_effect = DolphinSchedulerError("temporary scheduler failure")
    monkeypatch.setattr(versions, "DolphinSchedulerClient", factory)
    # SQLite omits timezone offsets; keep this regression focused on admission/state.
    monkeypatch.setattr(versions, "read_version", lambda record: {"phase": record.status})
    endpoint = f"/api/v1/projects/{project_id}/versions/{version_id}/submit"
    first = client.post(endpoint, json={})
    assert first.status_code == 200 and first.json()["phase"] == "submit_failed"
    scheduler.start.assert_called_once_with("model", str(directory / "input.json"))
    with Session(engine) as session:
        saved = session.get(ProjectVersion, version_id)
        accepted = deepcopy(saved.accepted_artifacts)
        assert upstream_name in accepted and saved.finished_at is not None
    if retired_source:
        kind, identity = retired_source.split("-")
        source = accepted[upstream_name]["sources"][kind]
        retired_policy = deepcopy(versions.policy.load_policy())
        retired_policy["retired_releases"][kind].append({
            "tag": source["tag"] if identity == "tag" else "unrelated-release",
            "commit": source["commit"] if identity == "commit" else "e" * 40,
            "reason": "upstream artifact source retired",
        })
        monkeypatch.setattr(versions.policy, "load_policy", lambda: retired_policy)
    assert client.post(f"/api/v1/version-policy/projects/{project_id}/check").json() == {"allowed": True}

    def no_capture(*args, **kwargs):
        pytest.fail("Retry must reuse the accepted artifact snapshots")

    monkeypatch.setattr(versions.policy, "capture_task_artifacts", no_capture)
    factory.reset_mock()
    scheduler.start.side_effect = None
    response = client.post(endpoint, json={})
    expected_status = "failed" if retired_source else "queued"
    if retired_source:
        assert response.status_code == 422, response.text
        assert response.json()["detail"] == {
            "code": "version_retired", "reason": "upstream artifact source retired",
        }
        factory.assert_not_called()
        scheduler.start.assert_not_called()
    else:
        assert response.status_code == 200 and response.json()["phase"] == "queued"
        scheduler.start.assert_called_once_with("model", str(directory / "input.json"))
    with Session(engine) as session:
        saved = session.get(ProjectVersion, version_id)
        assert saved.status == expected_status and saved.accepted_artifacts == accepted
        assert saved.error == ("upstream artifact source retired" if retired_source else "")
        assert (saved.finished_at is not None) == bool(retired_source)
        project = session.get(Project, project_id)
        assert versions.policy.project_sources(project) == original_sources
        assert project.artifact_dependencies == original_dependencies
    if retired_source:
        assert client.post(endpoint, json={}).json() == {"phase": "failed"}
        factory.assert_not_called()


def test_submission_checks_newly_captured_artifact_series(submission, monkeypatch):
    version, session, factory, _, _ = submission
    captured = {"model-abcd": {"package": "model-abcd", "version": "1.3.1", "kind": "model",
        "scheme_version": "1.3.0", "sources": versions.policy.project_sources(session.get.return_value)}}
    capture = MagicMock(return_value=captured)
    monkeypatch.setattr(versions.policy, "capture_task_artifacts", capture)
    with pytest.raises(HTTPException) as error:
        submit(version, session)
    assert error.value.status_code == 422 and "候选成果必须使用实际 Scheme 的主、次版本" in str(error.value.detail)
    assert version.accepted_artifacts is captured and version.status == "failed"
    assert version.error == error.value.detail and version.finished_at is not None
    capture.assert_called_once()
    factory.assert_not_called()
    session.commit.assert_called_once()


def test_explicit_rejection_can_retry(submission):
    version, session, _, client, _ = submission
    client.start.side_effect = DolphinSchedulerError("workflow offline")
    assert submit(version, session)["phase"] == "submit_failed"
    client.start.side_effect = None
    assert submit(version, session)["phase"] == "queued"
    assert client.start.call_count == 2


def test_unknown_submission_never_blindly_retries(submission):
    version, session, _, client, _ = submission
    client.start.side_effect = DolphinSchedulerError("response lost", submission_unknown=True)
    result = submit(version, session)
    assert result["phase"] == "queued"
    assert "待核实" in result["error"]
    assert submit(version, session)["phase"] == "queued"
    assert client.start.call_count == 1


def test_reconcile_finds_instance_beyond_first_page(submission):
    version, session, _, client, directory = submission
    version.status = "queued"
    client.instances.side_effect = [
        [{"id": i, "globalParams": "[]"} for i in range(100)],
        [{"id": 123, "globalParams": json.dumps([
            {"prop": "input_file", "value": str(directory / "input.json")},
        ])}],
    ]
    assert submit(version, session)["phase"] == "running"
    assert version.workflow_id == 123
    assert client.instances.call_args_list[1].kwargs == {"page": 2}
    client.start.assert_not_called()


@pytest.mark.parametrize("kind", ["model", "optimize", "control", "execution"])
def test_algo_submission_and_reconciliation_use_own_workflow(submission, kind):
    version, session, _, client, directory = submission
    session.get.return_value.kind = kind
    session.get.return_value.package_name = f"{kind}-{version.project_id.hex[:4]}"
    path = directory / "input.json"
    data = json.loads(path.read_text())
    component = data.pop("factor")
    package = f"{kind}-{version.project_id.hex[:4]}"
    component.update(package=package, entry=f"{package.replace('-', '_')}:Algo")
    with ZipFile(component["wheel"], "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA", f"Name: {package}\nVersion: 1.2.1\nRequires-Dist: scheme>=1.2.0,<1.3.0\n")
        archive.writestr(package.replace("-", "_") + "/__init__.py", "")
    (directory / "uv.lock").write_text(f'[[package]]\nname="scheme"\nversion="1.2.0"\n[[package]]\nname="{package}"\nversion="1.2.1"\nsource={{path="factor.whl"}}\n')
    data.update(kind=kind, algos={kind: component},
                backtest={"optimize": "risk_parity", "control": "no_control",
                          "execution": "direct_execution"})
    del data["analysis"]
    path.write_text(json.dumps(data))
    assert submit(version, session)["phase"] == "queued"
    client.start.assert_called_once_with(kind, str(path))
    assert version.parameters["backtest"] == data["backtest"]
    assert kind not in version.parameters["algos"]
    submit(version, session)
    client.instances.assert_called_with(kind, page=1)


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("workflow_id", [None, 42])
@pytest.mark.parametrize("filename,content", [
    ("input.json", None), ("input.json", "{broken"), ("input.json", "null"),
    ("uv.lock", None), ("uv.lock", "[[package]"),
])
def test_refresh_marks_missing_or_invalid_task_files_failed(submission, status, workflow_id, filename, content):
    version, _, factory, _, directory = submission
    version.status, version.workflow_id = status, workflow_id
    path = directory / filename
    if content is None:
        path.unlink()
    else:
        path.write_text(content, encoding="utf-8")
    versions.refresh_version(version, workflow_kind="factor")
    assert version.status == "failed"
    assert version.error.startswith("任务文件无效：") and len(version.error) > len("任务文件无效：")
    assert version.finished_at is not None
    factory.assert_not_called()


@pytest.mark.parametrize("content", [None, "{broken", "null", '{"status":"success"}',
                                      '{"status":"failed","versions":{"scheme":"1.2.0"}}'])
def test_refresh_marks_missing_or_invalid_success_report_failed(submission, content):
    version, _, _, client, directory = submission
    version.status, version.workflow_id = "running", 42
    report = directory / "report"
    report.mkdir()
    if content is not None:
        (report / "run.json").write_text(content, encoding="utf-8")
    client.instance.return_value = {"state": "SUCCESS"}
    versions.refresh_version(version, workflow_kind="factor")
    assert version.status == "failed"
    assert version.error.startswith("报告清单无效：")
    assert version.finished_at is not None
    client.instance.assert_called_once_with(42)


def test_refresh_accepts_success_report_without_hash_fields(submission):
    version, _, _, client, directory = submission
    version.status, version.workflow_id, version.scheme_version = "running", 42, "1.2.0"
    report = directory / "report"
    report.mkdir()
    (report / "run.json").write_text(json.dumps({
        "protocol": 1, "status": "success", "reports": {}, "versions": {"scheme": "1.2.0"},
    }), encoding="utf-8")
    client.instance.return_value = {"state": "SUCCESS"}
    versions.refresh_version(version, workflow_kind="factor")
    assert version.status == "success" and version.error == ""
    assert version.scheme_version == "1.2.0" and version.finished_at is not None


@pytest.mark.parametrize("status,workflow_id", [("queued", None), ("running", 42)])
def test_refresh_preserves_original_state_only_for_scheduler_unavailable(submission, status, workflow_id):
    version, _, factory, _, _ = submission
    version.status, version.workflow_id, version.error = status, workflow_id, "submission pending"
    factory.return_value.__enter__.side_effect = DolphinSchedulerError("temporarily unavailable")
    versions.refresh_version(version, workflow_kind="factor")
    assert version.status == status and version.workflow_id == workflow_id
    assert version.error == "submission pending" and version.finished_at is None


def test_refresh_does_not_hide_non_scheduler_errors(submission):
    version, _, _, client, _ = submission
    version.status, version.workflow_id = "running", 42
    client.instance.side_effect = ValueError("invalid scheduler payload")
    with pytest.raises(ValueError, match="invalid scheduler payload"):
        versions.refresh_version(version, workflow_kind="factor")


def test_reject_task_for_wrong_project_kind(submission):
    version, session, _, client, _ = submission
    session.get.return_value.kind = "model"
    with pytest.raises(HTTPException) as error:
        submit(version, session)
    assert error.value.status_code == 422
    client.start.assert_not_called()


def test_records_transitive_research_dependencies(tmp_path):
    length = 4
    factor = "factor-" + "a" * length
    model = "model-" + "b" * length
    current = "execution-" + "c" * length
    lock = tmp_path / "uv.lock"
    lock.write_text("\n".join(
        f'[[package]]\nname="{name}"\nversion="1.0.0"'
        for name in (factor, model, current, "pandas", "factor-unrelated")
    ), encoding="utf-8")
    assert versions.research_dependencies(lock, current) == [
        {"name": factor, "version": "1.0.0"},
        {"name": model, "version": "1.0.0"},
    ]


@pytest.mark.parametrize("start,end,expected", [
    (None, None, None),
    ("2026-09-26 15:00:00", None, 60),
    ("2026-09-26T07:00:00+00:00", None, 60),
    ("2026-09-26 15:00:00", "2026-09-26 15:00:30", 30),
    ("2026-09-26T15:00:00+08:00", "2026-09-26T07:00:30+00:00", 30),
    ("2026-09-26 15:00:00", "2026-09-26T07:00:30+00:00", 30),
    ("2026-09-26 15:02:00", None, 0),
])
def test_task_duration_uses_scheduler_timezone(monkeypatch, start, end, expected):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            instant = cls(2026, 9, 26, 7, 1, tzinfo=UTC)
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(versions, "datetime", Clock)
    assert versions.task_duration({"startTime": start, "endTime": end}) == expected
