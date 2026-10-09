"""策略组装的版本边界、参数冲突与冻结输入。"""

import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
import tomlkit
import tomllib
from core.apps.strategies import service
from core.apps.strategies import views as strategy_views
from fastapi import HTTPException
from pydantic import ValidationError


@pytest.mark.parametrize("kind,status,archived", [
    ("factor", "success", False), ("model", "failed", False), ("model", "success", True),
])
def test_selection_rejects_unusable_versions(kind, status, archived):
    session = MagicMock()
    session.get.side_effect = [SimpleNamespace(project_id=uuid4(), status=status), SimpleNamespace(kind=kind, archived=archived)]
    with pytest.raises(HTTPException, match="对应类型的成功研究版本"):
        service.selected(session, {"model": uuid4()})


def saved_result(tmp_path, kind, scheme):
    identifier, project_id = uuid4(), uuid4()
    series = ".".join(scheme.split(".")[:2])
    version = SimpleNamespace(project_id=project_id, id=identifier, status="success", number=1,
                              scheme_version=scheme, package_version=f"{series}.1",
                              input_sha256=None, lock_sha256=None)
    project = SimpleNamespace(id=project_id, name=kind, kind=kind, archived=False,
                              scheme_version=f"v{scheme}", scheme_commit="b" * 40,
                              template_tag=f"v{series}.0", template_commit="a" * 40)
    directory = tmp_path / "runs" / str(identifier)
    directory.mkdir(parents=True)
    package = f"{kind}-{project_id.hex[:4]}"
    (directory / "uv.lock").write_text(f'[[package]]\nname="scheme"\nversion="{scheme}"\n[[package]]\nname="{package}"\nversion="{version.package_version}"\nsource={{path="candidate.whl"}}\n')
    wheel = directory / "candidate.whl"
    with ZipFile(wheel, "w") as archive:
        archive.writestr(f"{package}.dist-info/METADATA", f"Name: {package}\nVersion: {version.package_version}\n"
                         f"Requires-Dist: scheme>={series}.0,<{series.split('.')[0]}.{int(series.split('.')[1]) + 1}.0\n")
        archive.writestr(package.replace("-", "_") + "/__init__.py", "")
    component = {"version": version.package_version, "package": package,
                 "wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
                 "entry": f"{package.replace('-', '_')}:Algo"}
    (directory / "input.json").write_text(json.dumps({
        "kind": kind, "algos": {kind: component},
        "environment": {"lockfile": str(directory / "uv.lock")},
    }))
    version.accepted_artifacts = {package: {
        **{key: component[key] for key in ("package", "version", "entry", "sha256")},
        "artifact_id": str(uuid4()), "kind": kind, "scheme_version": scheme,
        "sources": service.policy.project_sources(project),
    }}
    return version, project


@pytest.mark.parametrize("kind", service.KINDS[1:])
@pytest.mark.parametrize("model_scheme,component_scheme,compatible", [
    ("1.2.0", "1.2.0", True), ("1.2.0", "1.2.7", True), ("1.2.7", "1.2.0", True),
    ("1.2.0", "1.3.0", False), ("1.3.0", "1.2.7", False), ("1.2.0", "2.1.0", False),
])
def test_selection_requires_same_scheme_major_minor(tmp_path, monkeypatch, kind, model_scheme, component_scheme, compatible):
    monkeypatch.setattr(service.SoloSettings, "SHARED_DIR", tmp_path)
    session = MagicMock()
    model_result = saved_result(tmp_path, "model", model_scheme)
    component_result = saved_result(tmp_path, kind, component_scheme)
    session.get.side_effect = [*model_result, *component_result]
    session.scalars.return_value = [model_result[1], component_result[1]]
    session.scalar.return_value = None
    if not compatible:
        with pytest.raises(HTTPException, match="同一 Scheme 主、次版本") as error:
            service.selected(session, {"model": uuid4(), kind: uuid4()})
        assert error.value.status_code == 422
        return
    result = service.selected(session, {"model": uuid4(), kind: uuid4()})
    assert result["model"]["scheme_version"] == model_scheme
    assert result[kind]["scheme_version"] == component_scheme
    assert all(result[name]["version_id"] is None for name in service.KINDS if name not in {"model", kind})


def test_selection_preserves_supported_report_major(tmp_path, monkeypatch):
    monkeypatch.setattr(service.SoloSettings, "SHARED_DIR", tmp_path)
    session = MagicMock()
    result = saved_result(tmp_path, "model", "2.1.0")
    session.get.side_effect = result
    session.scalars.return_value = [result[1]]
    session.scalar.return_value = None
    with pytest.raises(HTTPException, match="支持 Scheme 1.x"):
        service.selected(session, {"model": uuid4()})


@pytest.mark.parametrize("body", [
    {}, {"artifacts": {}}, {"optimize": str(uuid4())},
    {"artifacts": {"factor": str(uuid4())}}, {"artifacts": {"model": None}},
    {"model": str(uuid4()), "artifacts": {"model": str(uuid4())}},
    {"model": str(uuid4()), "control": str(uuid4()), "artifacts": {"control": str(uuid4())}},
])
def test_selection_schema_rejects_missing_model_unknown_kind_and_conflicting_references(body):
    with pytest.raises(ValidationError):
        strategy_views.Selection.model_validate(body)


def test_selection_schema_supports_legacy_artifact_and_mixed_references():
    model, optimize, control = uuid4(), uuid4(), uuid4()
    legacy = strategy_views.Selection(model=model)
    assert legacy.artifacts == {}
    published = strategy_views.Selection(artifacts={"model": model, "optimize": optimize}, control=control)
    assert published.model is None
    assert published.artifacts == {"model": model, "optimize": optimize}
    assert published.control == control
    legacy.artifacts["execution"] = uuid4()
    assert strategy_views.Selection(model=model).artifacts == {}


@pytest.mark.parametrize("selections", [
    {}, {"artifacts": {}}, {"artifacts": {"factor": uuid4()}},
    {"model": uuid4(), "artifacts": {"model": uuid4()}},
    {"model": uuid4(), "execution": uuid4(), "artifacts": {"execution": uuid4()}},
])
def test_direct_selection_rejects_invalid_references_before_database_reads(selections):
    session = MagicMock()
    with pytest.raises(HTTPException) as error:
        service.selected(session, selections)
    assert error.value.status_code == 422
    session.get.assert_not_called()


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    wheel = source / "model_test-1.2.1-py3-none-any.whl"
    wheel.write_bytes(b"immutable candidate")
    (source / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": {
        "model-test": {"path": str(wheel)}, "scheme": {"git": "https://example.com/scheme", "rev": "f" * 40},
    }}}}))
    (source / "uv.lock").write_text('[[package]]\nname="model-test"\nversion="1.2.1"\n[[package]]\nname="scheme"\nversion="1.2.0"\n')
    components = {kind: {"version_id": None} for kind in service.KINDS}
    components["model"] = {
        "version_id": str(uuid4()), "scheme_version": "1.2.0",
        "root_sources": {"scheme": {"tag": "v1.2.0", "commit": "b" * 40},
                         "template": {"tag": "v1.2.0", "commit": "a" * 40}},
        "input": {"environment": {"lockfile": str(source / "uv.lock")}},
        "component": {"package": "model-test", "version": "1.2.1", "wheel": str(wheel),
                      "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "entry": "model_test:ModelAlgo"},
    }
    inspected = {
        "model": {"backtest": {"start": "2026-06-01", "model": "discard", "optimize": "discard"}, "params": {"n_select": 7}},
        "optimize": {"params": {"gross_exposure": 0.9}},
        "control": {"params": {"lot_size": 100}}, "execution": {"params": {}},
    }
    monkeypatch.setattr(service, "inspect_component", lambda _, kind, *args: inspected[kind])
    def freeze(args, cwd):
        (cwd / "uv.lock").write_text(f'[[package]]\nname="scheme"\nversion="{components["model"]["scheme_version"]}"\n')
        return ""

    command = MagicMock(side_effect=freeze)
    monkeypatch.setattr(service, "command", command)
    return components, {kind: {} for kind in service.KINDS}, inspected, tmp_path / "strategy", command


@pytest.mark.parametrize("scheme_version", ["1.2.0", "1.2.7", "1.2.99"])
def test_build_freezes_wheel_and_merges_each_algo_params(assembly, scheme_version):
    components, forms, _, destination, command = assembly
    components["model"]["scheme_version"] = scheme_version
    lock = components["model"]["input"]["environment"]["lockfile"]
    from pathlib import Path

    Path(lock).write_text(f'[[package]]\nname="scheme"\nversion="{scheme_version}"\n')
    assert service.build(components, forms, destination) == scheme_version
    data = json.loads((destination / "input.json").read_text())
    assert data["kind"] == "strategy"
    assert data["backtest"] == {"start": "2026-06-01", "n_select": 7, "gross_exposure": 0.9, "lot_size": 100}
    assert set(data["algos"]) == {"model"}  # 缺省环节由 Scheme 自动补齐。
    frozen = destination / "wheels/model-test/model_test-1.2.1-py3-none-any.whl"
    assert data["algos"]["model"]["wheel"] == str(frozen)
    assert frozen.read_bytes() == b"immutable candidate"
    config = tomllib.loads((destination / "environment/pyproject.toml").read_text())
    assert config["project"]["dependencies"] == ["model-test==1.2.1", f"scheme=={scheme_version}"]
    assert config["tool"]["uv"]["sources"]["scheme"] == {"git": "https://example.com/scheme", "rev": "f" * 40}
    assert config["tool"]["uv"]["constraint-dependencies"] == ["model-test", "scheme"]
    command.assert_called_once_with(["uv", "lock", "--project", str(destination / "environment")], destination / "environment")


@pytest.mark.parametrize("default_url", ["https://pypi.tuna.tsinghua.edu.cn/simple", "https://private.invalid/simple"])
def test_build_preserves_model_registry_policy_without_other_uv_settings(assembly, default_url):
    components, forms, _, destination, command = assembly
    original = destination.parent / "source/pyproject.toml"
    config = tomllib.loads(original.read_text())
    indexes = [{"name": "private", "url": "https://packages.invalid/simple", "explicit": True, "default": False,
                "format": "simple"}, {"name": "default", "url": default_url, "default": True, "explicit": False}]
    config["tool"]["uv"].update({"index": indexes + [{"name": "old-flat", "url": "../old-wheels", "format": "flat"}],
                               "index-strategy": "unsafe-best-match", "override-dependencies": ["unused==9"],
                               "dev-dependencies": ["unused-dev==9"]})
    config["tool"]["uv"]["sources"]["private-dep"] = {"index": "private"}
    original.write_text(tomlkit.dumps(config))
    assert service.build(components, forms, destination) == "1.2.0"
    uv = tomllib.loads((destination / "environment/pyproject.toml").read_text())["tool"]["uv"]
    assert uv["index"] == indexes
    assert uv["index-strategy"] == "unsafe-best-match"
    assert uv["sources"]["private-dep"] == {"index": "private"}
    assert uv["sources"]["scheme"] == config["tool"]["uv"]["sources"]["scheme"]
    assert set(uv) == {"package", "sources", "index", "index-strategy", "constraint-dependencies"}
    command.assert_called_once()


def test_build_preserves_legacy_model_registry_urls_without_tsinghua_override(assembly):
    components, forms, _, destination, _ = assembly
    original = destination.parent / "source/pyproject.toml"
    config = tomllib.loads(original.read_text())
    config["tool"]["uv"].update({"index-url": "https://private.invalid/simple",
                               "extra-index-url": ["https://packages.invalid/simple"],
                               "index-strategy": "unsafe-best-match"})
    original.write_text(tomlkit.dumps(config))
    assert service.build(components, forms, destination) == "1.2.0"
    uv = tomllib.loads((destination / "environment/pyproject.toml").read_text())["tool"]["uv"]
    assert uv["index"] == []
    for key in ("index-url", "extra-index-url", "index-strategy"):
        assert uv[key] == config["tool"]["uv"][key]


@pytest.mark.parametrize("indexes", [
    [], [{"name": "old-flat", "url": "../old-wheels", "format": "flat", "default": True}],
    [{"name": "private", "url": "https://packages.invalid/simple", "explicit": True}],
])
def test_build_defaults_to_tsinghua_without_explicit_model_default(assembly, indexes):
    components, forms, _, destination, _ = assembly
    original = destination.parent / "source/pyproject.toml"
    config = tomllib.loads(original.read_text())
    if indexes:
        config["tool"]["uv"]["index"] = indexes
    original.write_text(tomlkit.dumps(config))
    assert service.build(components, forms, destination) == "1.2.0"
    uv = tomllib.loads((destination / "environment/pyproject.toml").read_text())["tool"]["uv"]
    assert uv["index"] == [index for index in indexes if index.get("format") != "flat"] + [
        {"url": "https://pypi.tuna.tsinghua.edu.cn/simple", "default": True},
    ]
    assert "index-strategy" not in uv


@pytest.mark.parametrize("indexes", [
    [], [{"name": "private", "url": "../old-wheels", "format": "flat"}],
    [{"name": "private", "url": "https://one.invalid/simple"},
     {"name": "private", "url": "https://two.invalid/simple"}],
])
def test_build_rejects_missing_flat_or_ambiguous_named_registry(assembly, indexes):
    components, forms, _, destination, command = assembly
    original = destination.parent / "source/pyproject.toml"
    config = tomllib.loads(original.read_text())
    config["tool"]["uv"].update(index=indexes)
    config["tool"]["uv"]["sources"]["private-dep"] = {"index": "private"}
    original.write_text(tomlkit.dumps(config))
    with pytest.raises(ValueError, match="registry"):
        service.build(components, forms, destination)
    command.assert_not_called()
    assert not (destination / "input.json").exists()


@pytest.mark.parametrize("conflict", [False, True])
def test_build_preserves_component_named_source_without_importing_its_default_policy(assembly, conflict):
    components, forms, _, destination, command = assembly
    original = destination.parent / "source/pyproject.toml"
    config = tomllib.loads(original.read_text())
    if conflict:
        config["tool"]["uv"]["index"] = [{"name": "private", "url": "https://other.invalid/simple"}]
        original.write_text(tomlkit.dumps(config))
    source = destination.parent / "optimize"
    source.mkdir()
    wheel = source / "optimize_test-1.2.1-py3-none-any.whl"
    wheel.write_bytes(b"immutable optimize")
    uv = {"sources": {"scheme": config["tool"]["uv"]["sources"]["scheme"], "private-dep": {"index": "private"}},
          "index": [{"name": "private", "url": "https://packages.invalid/simple", "default": True, "explicit": False},
                    {"name": "unused", "url": "https://unused.invalid/simple"},
                    {"name": "old-flat", "url": "../old-wheels", "format": "flat"}],
          "index-strategy": "unsafe-best-match"}
    (source / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": uv}}))
    (source / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
    components["optimize"] = {**components["model"], "version_id": str(uuid4()),
                              "input": {"environment": {"lockfile": str(source / "uv.lock")}},
                              "component": {"package": "optimize-test", "version": "1.2.1", "wheel": str(wheel),
                                            "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "entry": "optimize_test:Algo"}}
    if conflict:
        with pytest.raises(ValueError, match="同名 registry.*配置冲突"):
            service.build(components, forms, destination)
        command.assert_not_called()
        return
    assert service.build(components, forms, destination) == "1.2.0"
    frozen = tomllib.loads((destination / "environment/pyproject.toml").read_text())["tool"]["uv"]
    assert frozen["index"] == [{"name": "private", "url": "https://packages.invalid/simple", "default": False, "explicit": True},
                              {"url": "https://pypi.tuna.tsinghua.edu.cn/simple", "default": True}]
    assert frozen["sources"]["private-dep"] == {"index": "private"}
    assert "index-strategy" not in frozen


@pytest.mark.parametrize("admitted", [True, False])
def test_create_strategy_captures_independent_descriptors_before_admission_and_queue(assembly, monkeypatch, admitted):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from core.apps.strategies.models import Strategy
    from core.database.base import Base

    components, forms, _, destination, _ = assembly
    artifact_id = uuid4()
    components["model"].update(artifact_id=str(artifact_id), version_id=None,
                               environment_path=str(destination.parent / "source"),
                               dependencies={"factor-abcd": {"artifact_id": str(uuid4())}})
    monkeypatch.setattr(strategy_views, "selected", lambda *args: components)
    monkeypatch.setattr(service.SoloSettings, "SHARED_DIR", destination.parent)
    scheduler = MagicMock()
    monkeypatch.setattr(strategy_views, "DolphinSchedulerClient", scheduler)
    snapshot = {"model-test": {"artifact_id": str(artifact_id), "sha256": components["model"]["component"]["sha256"]}}
    capture = MagicMock(return_value=snapshot)
    monkeypatch.setattr(service.policy, "capture_task_artifacts", capture)

    def check(session, strategy, **kwargs):
        assert strategy.accepted_artifacts == snapshot
        assert strategy.input_sha256 is None and strategy.lock_sha256 is None
        assert strategy.status == "building"
        if not admitted:
            raise ValueError("invalid accepted wheel snapshot")
        return False

    monkeypatch.setattr(service.policy, "check_task", check)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine, expire_on_commit=False) as session:
            response = strategy_views.create_strategy(strategy_views.CreateStrategy(
                name="independent", selections={"artifacts": {"model": artifact_id}}, forms=forms,
            ), session)
            strategy = session.get(Strategy, UUID(response["id"]))
            descriptor = strategy.components["model"]
            assert descriptor["artifact_id"] == str(artifact_id)
            assert descriptor["version_id"] is None
            assert descriptor["component_snapshot"] == {key: components["model"]["component"][key]
                                                       for key in ("package", "version", "entry", "sha256")}
            assert descriptor["dependencies"] == components["model"]["dependencies"]
            assert not {"input", "component", "environment_path"} & set(descriptor)
            assert str(destination.parent / "source") not in json.dumps(strategy.components)
            capture.assert_called_once_with(session, strategy, kind="strategy",
                                            directory=destination.parent / "runs" / str(strategy.id))
            assert strategy.accepted_artifacts == snapshot
            if admitted:
                assert response["status"] == "queued"
                assert strategy.input_sha256 and strategy.lock_sha256
                scheduler.return_value.__enter__.return_value.start.assert_called_once()
            else:
                assert response["status"] == "failed"
                assert strategy.input_sha256 is None and strategy.lock_sha256 is None
                scheduler.return_value.__enter__.return_value.start.assert_not_called()
    finally:
        engine.dispose()


def test_artifact_selection_uses_bundle_without_origin_reads_and_builds_frozen_copy(assembly, monkeypatch):
    from core.apps.artifacts import service as artifacts

    components, forms, _, destination, _ = assembly
    artifact_id = uuid4()
    model = {**components["model"], "artifact_id": str(artifact_id), "version_id": None, "published": True}
    resolve = MagicMock(return_value=model)
    monkeypatch.setattr(artifacts, "artifact_component", resolve)
    session = MagicMock()
    selected = service.selected(session, {"artifacts": {"model": artifact_id}})
    resolve.assert_called_once_with(session, artifact_id)
    session.get.assert_not_called()
    assert selected["model"]["version_id"] is None
    assert selected["model"]["artifact_id"] == str(artifact_id)
    assert service.build(selected, forms, destination) == "1.2.0"
    original = components["model"]["component"]["wheel"]
    from pathlib import Path

    Path(original).unlink()
    frozen = json.loads((destination / "input.json").read_text())["algos"]["model"]
    assert frozen["sha256"] == model["component"]["sha256"]
    assert Path(frozen["wheel"]).read_bytes() == b"immutable candidate"
    assert str(Path(original).parent) not in (destination / "input.json").read_text()


def test_artifact_form_syncs_bundle_environment_without_existing_venv(tmp_path, monkeypatch):
    environment = tmp_path / "artifacts/bundle/environment"
    environment.mkdir(parents=True)
    (environment / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
    components = {kind: {"version_id": None} for kind in service.KINDS}
    components["model"] = {
        "artifact_id": str(uuid4()), "version_id": None, "scheme_version": "1.2.0",
        "root_sources": {"scheme": {"tag": "v1.2.0", "commit": "b" * 40},
                         "template": {"tag": "v1.2.0", "commit": "a" * 40}},
        "component": {"version": "1.2.1", "entry": "model_abcd:ModelAlgo"},
        "input": {"environment": {"lockfile": str(environment / "uv.lock")}, "backtest": {"saved": True}},
    }
    command = MagicMock(side_effect=["", '{"values": {"restored": true}}'])
    monkeypatch.setattr(service, "command", command)
    assert not (environment / ".venv").exists()
    assert service.inspect_component(components, "model") == {"values": {"restored": True}}
    assert command.call_args_list[0].args == (["uv", "sync", "--project", str(environment), "--locked", "--no-dev"], environment)
    python_args, cwd, data = command.call_args_list[1].args
    assert python_args[0] == str(environment / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    assert cwd == environment
    assert data["entry"] == "model_abcd:ModelAlgo"
    assert data["saved"] == {"saved": True}


@pytest.mark.parametrize("tamper", ["kind", "artifact-id", "unpublished", "retired", "lock-scheme", "result-series"])
def test_artifact_selection_rejects_mismatched_or_retired_bundle_before_commands(assembly, monkeypatch, tamper):
    from core.apps.artifacts import service as artifacts
    from pathlib import Path

    components, _, _, destination, command = assembly
    artifact_id = uuid4()
    model = {**components["model"], "artifact_id": str(artifact_id), "version_id": None, "published": True}
    if tamper == "kind":
        model["kind"] = "optimize"
    elif tamper == "artifact-id":
        model["artifact_id"] = str(uuid4())
    elif tamper == "unpublished":
        model["published"] = False
    elif tamper == "retired":
        model["root_sources"] = {**model["root_sources"], "template": {
            "tag": "v1.2.0", "commit": service.policy.load_policy()["retired_releases"]["template"][0]["commit"],
        }}
    elif tamper == "lock-scheme":
        Path(model["input"]["environment"]["lockfile"]).write_text('[[package]]\nname="scheme"\nversion="1.2.7"\n')
    else:
        model["component"] = {**model["component"], "version": "1.3.1"}
    monkeypatch.setattr(artifacts, "artifact_component", lambda *args: model)
    session = MagicMock()
    with pytest.raises(HTTPException) as error:
        service.selected(session, {"artifacts": {"model": artifact_id}})
    assert error.value.status_code == 422
    if tamper == "retired":
        assert error.value.detail["code"] == "version_retired"
    command.assert_not_called()
    session.get.assert_not_called()
    assert not destination.exists()


@pytest.mark.parametrize("model_scheme,component_scheme,compatible", [
    ("1.2.0", "1.2.7", True), ("1.2.7", "1.2.0", True), ("1.2.0", "1.3.0", False),
])
@pytest.mark.parametrize("kind", service.KINDS[1:])
def test_artifact_selection_preserves_same_scheme_series(tmp_path, monkeypatch, model_scheme, component_scheme, compatible, kind):
    from core.apps.artifacts import service as artifacts

    monkeypatch.setattr(service.SoloSettings, "SHARED_DIR", tmp_path)
    candidates, identifiers = {}, {}
    for stage, scheme in (("model", model_scheme), (kind, component_scheme)):
        version, project = saved_result(tmp_path, stage, scheme)
        source = json.loads((service.version_directory(version) / "input.json").read_text())
        identifier = uuid4()
        identifiers[stage] = identifier
        candidates[identifier] = {
            "label": stage, "kind": stage, "artifact_id": str(identifier), "version_id": None,
            "scheme_version": scheme, "root_sources": service.policy.project_sources(project),
            "component": source["algos"][stage], "input": source,
        }
    monkeypatch.setattr(artifacts, "artifact_component", lambda session, identifier: candidates[identifier])
    session = MagicMock()
    if compatible:
        selected = service.selected(session, {"artifacts": identifiers})
        assert selected["model"]["scheme_version"] == model_scheme
        assert selected[kind]["scheme_version"] == component_scheme
    else:
        with pytest.raises(HTTPException, match="同一 Scheme 主、次版本"):
            service.selected(session, {"artifacts": identifiers})
    session.get.assert_not_called()


def test_selection_allows_published_model_with_live_legacy_component(tmp_path, monkeypatch):
    from core.apps.artifacts import service as artifacts

    monkeypatch.setattr(service.SoloSettings, "SHARED_DIR", tmp_path)
    model, model_project = saved_result(tmp_path, "model", "1.2.7")
    legacy, legacy_project = saved_result(tmp_path, "optimize", "1.2.0")
    source = json.loads((service.version_directory(model) / "input.json").read_text())
    artifact_id = uuid4()
    published = {"label": "published", "artifact_id": str(artifact_id), "version_id": None,
                 "scheme_version": model.scheme_version, "root_sources": service.policy.project_sources(model_project),
                 "component": source["algos"]["model"], "input": source}
    monkeypatch.setattr(artifacts, "artifact_component", lambda *args: published)
    session = MagicMock()
    session.get.side_effect = [legacy, legacy_project]
    selected = service.selected(session, {"artifacts": {"model": artifact_id}, "optimize": legacy.id})
    assert selected["model"]["artifact_id"] == str(artifact_id)
    assert selected["model"]["version_id"] is None
    assert selected["optimize"]["version_id"] == str(legacy.id)
    assert selected["optimize"]["artifact_id"] is None
    assert session.get.call_count == 2


def test_build_rejects_conflicting_params_before_writing(assembly):
    components, forms, inspected, destination, command = assembly
    inspected["control"]["params"]["gross_exposure"] = 0.5
    with pytest.raises(ValueError, match="同名参数 gross_exposure"):
        service.build(components, forms, destination)
    assert not destination.exists()
    command.assert_not_called()


def test_build_rejects_modified_wheel(assembly):
    components, forms, _, destination, command = assembly
    components["model"]["component"]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="哈希不一致"):
        service.build(components, forms, destination)
    command.assert_not_called()


def test_forms_restore_saved_universe_lookback(monkeypatch):
    from datetime import timedelta
    from types import ModuleType

    from core.apps.strategies import parameters
    from pydantic import BaseModel

    class Form(BaseModel):
        pool: str = "沪深 300"
        lookback: timedelta = timedelta(0)

    assembly = ModuleType("scheme.execute.strategy.assembly")
    assembly.parameter_type = lambda _: BaseModel
    module = ModuleType("model_example")
    module.ModelAlgo, module.ModelReportForm = object, Form
    monkeypatch.setitem(sys.modules, assembly.__name__, assembly)
    monkeypatch.setitem(sys.modules, module.__name__, module)
    result = parameters.inspect({"kind": "model", "entry": "model_example:ModelAlgo",
                                 "saved": {"universe": {"pool": "上证 50", "lookback": "P60D"}}})
    assert Form.model_validate(result["values"]).lookback == timedelta(days=60)
    assert result["values"]["pool"] == "上证 50"


@pytest.fixture
def local_wheels(tmp_path):
    wheels = tmp_path / "available"
    wheels.mkdir()

    def wheel(name, version, requires=(), files=None):
        path = wheels / f"{name}-{version}-py3-none-any.whl"
        metadata = f"{name}-{version}.dist-info"
        with ZipFile(path, "w") as archive:
            archive.writestr(f"{metadata}/METADATA", f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                             + "".join(f"Requires-Dist: {value}\n" for value in requires))
            archive.writestr(f"{metadata}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
            archive.writestr(f"{metadata}/RECORD", "")
            for name, content in (files or {}).items():
                archive.writestr(name.replace("DIST_INFO", metadata), content)
        return path

    return wheels, wheel


@pytest.fixture
def offline_uv(tmp_path, monkeypatch, local_wheels):
    wheels, _ = local_wheels
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(service.SoloSettings, "HOME_DIR", home)

    def offline(args, cwd, data=None):
        assert args[:2] in (["uv", "lock"], ["uv", "sync"])
        assert cwd.resolve().is_relative_to(tmp_path.resolve())
        args = [*args, "--offline", "--no-cache", "--find-links", str(wheels),
                "--python", sys.executable, "--no-python-downloads"]
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "UV_PROJECT_ENVIRONMENT": str(cwd / ".venv"), "UV_PYTHON_INSTALL_DIR": str(home / ".python"),
               "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_NO_CACHE": "1", "UV_OFFLINE": "1",
               "UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1"}
        for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
        result = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True, timeout=120, check=False)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout)[-4000:])
        return result.stdout

    monkeypatch.setattr(service, "command", offline)
    return offline


@pytest.mark.parametrize("factor_requirements,expected", [
    ((">=1.2.1,<1.3.0", ">=1.2.2,<1.3.0"), "1.2.2"),
    ((">=1.2.1,<1.2.2", ">=1.2.1,<1.3.0"), "1.2.1"),
    (("==1.2.1", "==1.2.2"), None),
])
@pytest.mark.parametrize("model_scheme,optimize_scheme", [("1.2.0", "1.2.7"), ("1.2.7", "1.2.0")])
@pytest.mark.parametrize("reverse", [False, True])
def test_assembly_resolves_shared_dependencies(tmp_path, monkeypatch, local_wheels, offline_uv,
                                              factor_requirements, expected, model_scheme, optimize_scheme, reverse):
    _, wheel = local_wheels
    schemes = {version: wheel("scheme", version) for version in ("1.2.0", "1.2.7", "1.2.99")}
    wheel("shared_dep", "1.0.0")
    wheel("shared_dep", "2.0.0")
    components = {kind: {"version_id": None} for kind in service.KINDS}
    for i, (kind, requirement, old_version, scheme_version) in enumerate((
        ("model", ">=1,<3", "1.0.0", model_scheme), ("optimize", ">=2,<3", "2.0.0", optimize_scheme),
    )):
        factor = wheel("factor_abcd", f"1.2.{i + 1}", ["scheme>=1.2.0,<1.3.0"])
        factor = factor.rename(tmp_path / factor.name)
        candidate = wheel(f"{kind}_demo", "1.2.3", [f"shared-dep{requirement}", "scheme>=1.2.0,<1.3.0",
                                                   f"factor-abcd{factor_requirements[i]}"])
        environment = tmp_path / kind
        environment.mkdir()
        (environment / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": {
            "factor-abcd": {"path": str(factor)}, "scheme": {"path": str(schemes[scheme_version])},
        }}}}))
        (environment / "uv.lock").write_text(
            f'[[package]]\nname="shared-dep"\nversion="{old_version}"\n'
            f'[[package]]\nname="scheme"\nversion="{scheme_version}"\n'
        )
        components[kind] = {
            "version_id": str(uuid4()), "scheme_version": scheme_version,
            "root_sources": {"scheme": {"tag": f"v{scheme_version}", "commit": "b" * 40},
                             "template": {"tag": "v1.2.0", "commit": "a" * 40}},
            "input": {"environment": {"lockfile": str(environment / "uv.lock")}},
            "component": {"package": f"{kind}-demo", "version": "1.2.3", "wheel": str(candidate),
                          "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(), "entry": f"{kind}_demo:Algo"},
        }
    monkeypatch.setattr(service, "inspect_component", lambda *args: {"params": {}, "backtest": {}})
    destination = tmp_path / "assembled"
    if reverse:
        components = dict(reversed(list(components.items())))
    if expected is None:
        with pytest.raises(ValueError, match="No solution"):
            service.build(components, {kind: {} for kind in service.KINDS}, destination)
        assert not (destination / "input.json").exists()
        return
    assert service.build(components, {kind: {} for kind in service.KINDS}, destination) == model_scheme
    environment = destination / "environment"
    config = tomllib.loads((environment / "pyproject.toml").read_text())
    assert f"scheme=={model_scheme}" in config["project"]["dependencies"]
    assert config["tool"]["uv"]["sources"]["scheme"] == {
        "path": str(destination / "wheels" / "scheme" / schemes[model_scheme].name),
    }
    lock_path = environment / "uv.lock"
    frozen_lock = lock_path.read_bytes()
    lock = tomllib.loads(frozen_lock.decode())
    assert next(p["version"] for p in lock["package"] if p["name"] == "shared-dep") == "2.0.0"
    assert next(p["version"] for p in lock["package"] if p["name"] == "factor-abcd") == expected
    assert next(p["version"] for p in lock["package"] if p["name"] == "scheme") == model_scheme
    assert next(p["version"] for p in lock["package"] if p["name"] == "model-demo") == "1.2.3"
    # 原始候选消失后仍可安装；更高 Scheme patch 候选不能改变 Model 的冻结版本。
    for factor in tmp_path.glob("factor_abcd-*.whl"):
        factor.unlink()
    for scheme in schemes.values():
        scheme.unlink()
    offline_uv(["uv", "sync", "--locked", "--no-dev"], environment)
    assert lock_path.read_bytes() == frozen_lock
    assert json.loads((destination / "input.json").read_text())["kind"] == "strategy"
