"""保存任务中断、调度重试和重复请求的状态回归。"""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
from config import SoloSettings
from core.apps.projects import versions
from core.apps.projects.models import ProjectVersion
from core.scheduler.client import DolphinSchedulerError
from fastapi import HTTPException


@pytest.fixture
def submission(tmp_path, monkeypatch):
    version = ProjectVersion(
        id=uuid4(), project_id=uuid4(), number=1, package_version="1.2.1",
        status="building", parameters={}, error="", note="",
        created_at=datetime.now(UTC),
    )
    session = MagicMock()
    session.scalar.return_value = version
    session.get.return_value = SimpleNamespace(id=version.project_id, kind="factor", archived=False, scheme_version="v1.2.0",
                                               scheme_commit="b" * 40, template_tag="v1.2.0", template_commit="a" * 40)
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
                   "entry": f"{package.replace('-', '_')}:Factor", "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest()},
        "environment": {"lockfile": str(directory / "uv.lock")}, "analysis": {},
    }), encoding="utf-8")
    (directory / "build.json").write_text('{"scheme_version":"1.2.0"}', encoding="utf-8")
    return version, session, factory, client, directory


def submit(version, session):
    return versions.submit_version(version.project_id, version.id, versions.BuildResult(), session)


@pytest.mark.parametrize("scheme_version,series", [("1.2.0", "1.2"), ("1.2.7", "1.2"), ("1.2.99", "1.2")])
@pytest.mark.parametrize("previous_number", [0, 7])
def test_create_version_uses_actual_locked_scheme_series(tmp_path, monkeypatch, scheme_version, series, previous_number):
    project = SimpleNamespace(id=uuid4(), archived=False, scheme_version="v9.9.9", scheme_commit="b" * 40,
                              template_tag="v1.2.0", template_commit="a" * 40)
    lock = tmp_path / "uv.lock"
    original = f'[[package]]\nname="scheme"\nversion="{scheme_version}"\n'
    lock.write_text(original, encoding="utf-8")
    monkeypatch.setattr(versions, "project_directory", lambda _: tmp_path)
    monkeypatch.setattr(versions, "read_version", lambda version: {"packageVersion": version.package_version})
    session = MagicMock()
    session.scalar.side_effect = [project, None, previous_number]
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
    project = SimpleNamespace(id=uuid4(), archived=False, scheme_version="v1.2.0", scheme_commit="b" * 40,
                              template_tag="v1.2.0", template_commit="a" * 40)
    if content is not None:
        (tmp_path / "uv.lock").write_text(content, encoding="utf-8")
    monkeypatch.setattr(versions, "project_directory", lambda _: tmp_path)
    session = MagicMock()
    session.scalar.side_effect = [project, None, None]
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
    path = directory / "input.json"
    data = json.loads(path.read_text())
    component = data.pop("factor")
    package = f"{kind}-{version.project_id.hex[:4]}"
    component.update(package=package, entry=f"{package.replace('-', '_')}:Algo")
    with ZipFile(component["wheel"], "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA", f"Name: {package}\nVersion: 1.2.1\nRequires-Dist: scheme>=1.2.0,<1.3.0\n")
        archive.writestr(package.replace("-", "_") + "/__init__.py", "")
    component["sha256"] = hashlib.sha256(Path(component["wheel"]).read_bytes()).hexdigest()
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
