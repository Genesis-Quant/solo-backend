"""Strategy assembly, validation, parameters and request-scoped environment reuse."""

import json
import tomllib
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
import tomlkit
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.strategies import service, views
from core.apps.strategies.models import Strategy
from core.database.base import Base


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    wheel = source / "model_test-1.2.1-py3-none-any.whl"
    wheel.write_bytes(b"local candidate")
    (source / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": {
        "model-test": {"path": str(wheel)}, "scheme": [{"git": "https://example.invalid/scheme", "rev": "b" * 40}],
    }}}}))
    (source / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
    components = {kind: {"version_id": None} for kind in service.KINDS}
    components["model"] = {"version_id": str(uuid4()), "scheme_version": "1.2.0",
        "accepted_artifacts": {"model-test": {"id": str(uuid4())}},
        "root_sources": {"scheme": {"tag": "v1.2.0", "commit": "b" * 40},
                         "template": {"tag": "v1.2.0", "commit": "a" * 40}},
        "input": {"environment": {"lockfile": str(source / "uv.lock")}},
        "component": {"package": "model-test", "version": "1.2.1", "wheel": str(wheel), "entry": "model_test:ModelAlgo"}}
    inspected = {
        "model": {"backtest": {"start": "2026-06-01", "optimize": "discard"}, "params": {"n_select": 7}},
        "optimize": {"params": {"gross_exposure": 0.9}}, "control": {"params": {"lot_size": 100}}, "execution": {"params": {}},
    }
    calls = []
    def command(args, cwd, data=None):
        calls.append((args, cwd, data))
        if args[:2] == ["uv", "lock"]:
            (cwd / "uv.lock").write_text(f'[[package]]\nname="scheme"\nversion="{components["model"]["scheme_version"]}"\n')
            return ""
        if args[:2] == ["uv", "sync"]:
            return ""
        return json.dumps(deepcopy(inspected[data["kind"]]))
    monkeypatch.setattr(service, "command", command)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    return components, {kind: {} for kind in service.KINDS}, inspected, tmp_path / "strategy", calls


@pytest.mark.parametrize("body", [{}, {"optimize": str(uuid4())}, {"artifacts": {"factor": str(uuid4())}},
    {"model": str(uuid4()), "artifacts": {"model": str(uuid4())}}])
def test_selection_schema_rejects_invalid_references(body):
    with pytest.raises(ValidationError):
        views.Selection.model_validate(body)


@pytest.mark.parametrize("scheme", ["1.2.0", "1.2.7", "1.2.99"])
def test_build_keeps_wire_without_hashes_and_syncs_environment_once(assembly, monkeypatch, scheme):
    components, forms, _, destination, calls = assembly
    components["model"]["scheme_version"] = scheme
    Path(components["model"]["input"]["environment"]["lockfile"]).write_text(f'[[package]]\nname="scheme"\nversion="{scheme}"\n')
    original = service.require_components
    validation = MagicMock(side_effect=original)
    monkeypatch.setattr(service, "require_components", validation)
    assert service.build(components, forms, destination) == scheme
    validation.assert_called_once_with(components)
    assert sum(args[:2] == ["uv", "sync"] for args, *_ in calls) == 1
    data = json.loads((destination / "input.json").read_text())
    assert data["backtest"] == {"start": "2026-06-01", "n_select": 7, "gross_exposure": 0.9, "lot_size": 100}
    assert set(data["algos"]["model"]) == {"package", "version", "wheel", "entry"}
    assert Path(data["algos"]["model"]["wheel"]).is_file()
    assert "sha256" not in json.dumps(data)
    config = tomllib.loads((destination / "environment/pyproject.toml").read_text())
    assert f"scheme=={scheme}" in config["project"]["dependencies"]


def test_forms_share_one_component_validation_and_one_sync(assembly, monkeypatch):
    components, _, _, _, calls = assembly
    monkeypatch.setattr(views, "selected", lambda *args: components)
    original = service.require_components
    validation = MagicMock(side_effect=original)
    monkeypatch.setattr(service, "require_components", validation)
    assert set(views.forms(views.Selection(model=uuid4()), MagicMock())) == set(service.KINDS)
    validation.assert_called_once()
    assert sum(args[:2] == ["uv", "sync"] for args, *_ in calls) == 1


def test_named_flat_index_and_source_lists_are_supported(assembly):
    components, forms, _, destination, _ = assembly
    path = Path(components["model"]["input"]["environment"]["lockfile"]).with_name("pyproject.toml")
    config = tomllib.loads(path.read_text())
    config["tool"]["uv"].update(index=[{"name": "private", "url": "https://private.invalid/wheels", "format": "flat", "default": True}])
    config["tool"]["uv"]["sources"]["helper"] = [{"index": "private", "marker": "sys_platform == 'win32'"},
                                                     {"url": "https://example.invalid/helper.whl", "marker": "sys_platform != 'win32'"}]
    path.write_text(tomlkit.dumps(config))
    service.build(components, forms, destination)
    frozen = tomllib.loads((destination / "environment/pyproject.toml").read_text())["tool"]["uv"]
    assert frozen["index"] == config["tool"]["uv"]["index"]
    assert frozen["sources"]["helper"] == config["tool"]["uv"]["sources"]["helper"]


@pytest.mark.parametrize("failure", ["retired", "series", "major"])
def test_component_contract_fails_before_environment_sync(assembly, failure):
    components, forms, _, destination, calls = assembly
    model = components["model"]
    if failure == "retired":
        model["root_sources"]["template"]["tag"] = "v1.0.1"
    elif failure == "series":
        model["component"]["version"] = "1.3.1"
    else:
        model["scheme_version"] = "2.1.0"
        model["component"]["version"] = "2.1.1"
        Path(model["input"]["environment"]["lockfile"]).write_text('[[package]]\nname="scheme"\nversion="2.1.0"\n')
    with pytest.raises(ValueError):
        service.build(components, forms, destination)
    assert calls == [] and not destination.exists()


def test_conflicting_parameter_values_fail_before_writing(assembly):
    components, forms, inspected, destination, _ = assembly
    inspected["control"]["params"]["gross_exposure"] = 0.5
    with pytest.raises(ValueError, match="同名参数"):
        service.build(components, forms, destination)
    assert not destination.exists()


def test_strategy_creation_persists_uuid_descriptors_and_submits(assembly, monkeypatch):
    components, forms, _, destination, calls = assembly
    artifact_id = uuid4()
    components["model"].update(artifact_id=str(artifact_id), version_id=None)
    monkeypatch.setattr(views, "selected", lambda *args: components)
    snapshots = {"model-test": {"id": str(artifact_id), "artifact_id": str(artifact_id), "package": "model-test",
                              "version": "1.2.1", "kind": "model"}}
    monkeypatch.setattr(policy, "capture_task_artifacts", lambda *args, **kwargs: snapshots)
    scheduler = MagicMock()
    monkeypatch.setattr(views, "DolphinSchedulerClient", scheduler)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        response = views.create_strategy(views.CreateStrategy(name="assembled", selections={"artifacts": {"model": artifact_id}}, forms=forms), session)
        strategy = session.get(Strategy, UUID(response["id"]))
        assert strategy.status == "queued" and strategy.accepted_artifacts == snapshots
        assert set(strategy.components["model"]["component_snapshot"]) == {"package", "version", "entry"}
        assert not {"input", "component", "environment_path"} & strategy.components["model"].keys()
        scheduler.return_value.__enter__.return_value.start.assert_called_once()
        assert sum(args[:2] == ["uv", "sync"] for args, *_ in calls) == 1
    engine.dispose()


@pytest.mark.parametrize("reverse", [False, True])
def test_build_collects_multiple_local_candidates_from_selected_components(assembly, reverse):
    components, forms, _, destination, calls = assembly
    model = components["model"]
    model["accepted_artifacts"]["helper"] = {"id": str(uuid4())}
    model_environment = Path(model["input"]["environment"]["lockfile"]).parent
    first = model_environment / "helper-1.0.0-py3-none-any.whl"
    first.write_bytes(b"first helper")
    model_config = tomllib.loads((model_environment / "pyproject.toml").read_text())
    model_config["tool"]["uv"]["sources"]["helper"] = [{"path": first.name}]
    (model_environment / "pyproject.toml").write_text(tomlkit.dumps(model_config))
    optimize_environment = destination.parent / "optimize"
    optimize_environment.mkdir()
    second = optimize_environment / "helper-2.0.0-py3-none-any.whl"
    second.write_bytes(b"second helper")
    wheel = optimize_environment / "optimize_test-1.2.1-py3-none-any.whl"
    wheel.write_bytes(b"optimize candidate")
    (optimize_environment / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
    (optimize_environment / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": {
        "helper": [{"path": second.name}], "scheme": model_config["tool"]["uv"]["sources"]["scheme"],
    }}}}))
    components["optimize"] = {**deepcopy(model), "version_id": str(uuid4()),
        "input": {"environment": {"lockfile": str(optimize_environment / "uv.lock")}},
        "component": {"package": "optimize-test", "version": "1.2.1", "entry": "optimize_test:OptimizeAlgo", "wheel": str(wheel)}}
    components["optimize"]["accepted_artifacts"] = {
        "optimize-test": {"id": str(uuid4())}, "helper": {"id": str(uuid4())},
    }
    if reverse:
        components = dict(reversed(list(components.items())))
    service.build(components, forms, destination)
    uv = tomllib.loads((destination / "environment/pyproject.toml").read_text())["tool"]["uv"]
    directory = destination / "wheels/helper"
    assert uv["sources"]["helper"] == {"index": "wheels-helper"}
    assert {"name": "wheels-helper", "url": str(directory), "format": "flat"} in uv["index"]
    assert {path.name for path in directory.iterdir()} == {first.name, second.name}
    assert all((directory / wheel.name).is_file() for wheel in (first, second))
    assert sum(args[:2] == ["uv", "sync"] for args, *_ in calls) == 2


@pytest.mark.parametrize("shared", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_filename_candidates_use_accepted_uuid_identity(assembly, monkeypatch, shared, reverse):
    components, forms, _, destination, _ = assembly
    model = components["model"]
    model_environment = Path(model["input"]["environment"]["lockfile"]).parent
    optimize_environment = destination.parent / "optimize"
    optimize_environment.mkdir()
    candidates = []
    identifier = str(uuid4())
    for kind, environment in (("model", model_environment), ("optimize", optimize_environment)):
        wheel = environment / "factor_abcd-1.2.1-py3-none-any.whl"
        wheel.write_bytes(b"upstream candidate")
        candidates.append(wheel)
        if kind == "model":
            config = tomllib.loads((environment / "pyproject.toml").read_text())
        else:
            root = environment / "optimize_test-1.2.1-py3-none-any.whl"
            root.write_bytes(b"optimize candidate")
            (environment / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
            components[kind] = {**deepcopy(model), "version_id": str(uuid4()),
                "accepted_artifacts": {"optimize-test": {"id": str(uuid4())}},
                "input": {"environment": {"lockfile": str(environment / "uv.lock")}},
                "component": {"package": "optimize-test", "version": "1.2.1",
                              "entry": "optimize_test:OptimizeAlgo", "wheel": str(root)}}
            config = {"tool": {"uv": {"sources": {"scheme": {"git": "https://example.invalid/scheme", "rev": "b" * 40}}}}}
        config["tool"]["uv"]["sources"]["factor-abcd"] = {"path": str(wheel)}
        (environment / "pyproject.toml").write_text(tomlkit.dumps(config))
        components[kind]["accepted_artifacts"]["factor-abcd"] = {"id": identifier if shared or kind == "model" else str(uuid4())}
    if reverse:
        components = dict(reversed(list(components.items())))
    copies = MagicMock(wraps=service.shutil.copy2)
    monkeypatch.setattr(service.shutil, "copy2", copies)
    if not shared:
        with pytest.raises(ValueError, match="同名 wheel 来源冲突"):
            service.build(components, forms, destination)
        assert not (destination / "input.json").exists()
        return
    service.build(components, forms, destination)
    target = destination / "wheels/factor-abcd" / candidates[0].name
    uv = tomllib.loads((destination / "environment/pyproject.toml").read_text())["tool"]["uv"]
    assert uv["sources"]["factor-abcd"] == {"path": str(target)}
    assert list(target.parent.iterdir()) == [target]
    upstream = [call.args for call in copies.call_args_list if call.args[1] == target]
    assert upstream == [(candidates[int(reverse)], target)]


def test_selection_rejects_failed_or_archived_versions():
    for archived, status in ((True, "success"), (False, "failed")):
        session = MagicMock()
        session.get.side_effect = [MagicMock(project_id=uuid4(), status=status), MagicMock(kind="model", archived=archived)]
        with pytest.raises(HTTPException):
            service.selected(session, {"model": uuid4()})
