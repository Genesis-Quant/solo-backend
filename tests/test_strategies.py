"""策略组装的版本边界、参数冲突与冻结输入。"""

import hashlib
import json
import tomllib
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
import tomlkit
from fastapi import HTTPException

from core.apps.strategies import service


@pytest.mark.parametrize("kind,status,archived", [
    ("factor", "success", False), ("model", "failed", False), ("model", "success", True),
])
def test_selection_rejects_unusable_versions(kind, status, archived):
    session = MagicMock()
    session.get.side_effect = [SimpleNamespace(project_id=uuid4(), status=status), SimpleNamespace(kind=kind, archived=archived)]
    with pytest.raises(HTTPException, match="对应类型的成功研究版本"):
        service.selected(session, {"model": uuid4()})


def test_selection_rejects_different_scheme_major(tmp_path, monkeypatch):
    (tmp_path / "input.json").write_text(json.dumps({"algos": {"model": {}, "optimize": {}}}))
    monkeypatch.setattr(service, "version_directory", lambda _: tmp_path)
    session = MagicMock()
    session.get.side_effect = [
        SimpleNamespace(project_id=uuid4(), id=uuid4(), status="success", number=1, scheme_version="1.2.0"),
        SimpleNamespace(id=uuid4(), name="model", kind="model", archived=False),
        SimpleNamespace(project_id=uuid4(), id=uuid4(), status="success", number=1, scheme_version="2.0.0"),
        SimpleNamespace(id=uuid4(), name="optimize", kind="optimize", archived=False),
    ]
    with pytest.raises(HTTPException, match="同一 Scheme 大版本"):
        service.selected(session, {"model": uuid4(), "optimize": uuid4()})


@pytest.fixture
def assembly(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    wheel = source / "model_test-1.0.0-py3-none-any.whl"
    wheel.write_bytes(b"immutable candidate")
    (source / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": {
        "model-test": {"path": str(wheel)}, "scheme": {"git": "https://example.com/scheme", "rev": "f" * 40},
    }}}}))
    (source / "uv.lock").write_text('[[package]]\nname="model-test"\nversion="1.0.0"\n')
    components = {kind: {"version_id": None} for kind in service.KINDS}
    components["model"] = {
        "version_id": str(uuid4()), "scheme_version": "1.0.0",
        "input": {"environment": {"lockfile": str(source / "uv.lock")}},
        "component": {"package": "model-test", "version": "1.0.0", "wheel": str(wheel),
                      "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(), "entry": "model_test:ModelAlgo"},
    }
    inspected = {
        "model": {"backtest": {"start": "2026-06-01", "model": "discard", "optimize": "discard"}, "params": {"n_select": 7}},
        "optimize": {"params": {"gross_exposure": 0.9}},
        "control": {"params": {"lot_size": 100}}, "execution": {"params": {}},
    }
    monkeypatch.setattr(service, "inspect_component", lambda _, kind, *args: inspected[kind])
    command = MagicMock(return_value="")
    monkeypatch.setattr(service, "command", command)
    return components, {kind: {} for kind in service.KINDS}, inspected, tmp_path / "strategy", command


def test_build_freezes_wheel_and_merges_each_algo_params(assembly):
    components, forms, _, destination, command = assembly
    service.build(components, forms, destination)
    data = json.loads((destination / "input.json").read_text())
    assert data["kind"] == "strategy"
    assert data["backtest"] == {"start": "2026-06-01", "n_select": 7, "gross_exposure": 0.9, "lot_size": 100}
    assert set(data["algos"]) == {"model"}  # 缺省环节由 Scheme 自动补齐。
    frozen = destination / "wheels/model-test/model_test-1.0.0-py3-none-any.whl"
    assert data["algos"]["model"]["wheel"] == str(frozen)
    assert frozen.read_bytes() == b"immutable candidate"
    config = tomllib.loads((destination / "environment/pyproject.toml").read_text())
    assert config["project"]["dependencies"] == ["model-test==1.0.0", "scheme==1.0.0"]
    assert config["tool"]["uv"]["constraint-dependencies"] == ["model-test", "scheme"]
    assert command.call_count == 1


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
    import sys
    from datetime import timedelta
    from types import ModuleType
    from pydantic import BaseModel
    from core.apps.strategies import parameters

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


@pytest.mark.parametrize("factor_requirements,expected", [
    ((">=1.0.1,<2", ">=1.0.2,<2"), "1.0.2"),
    ((">=1.0.1,<1.0.2", ">=1.0.1,<2"), "1.0.1"),
    (("==1.0.1", "==1.0.2"), None),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_assembly_resolves_shared_dependencies(tmp_path, monkeypatch, factor_requirements, expected, reverse):
    import sys

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

    scheme = wheel("scheme", "1.0.0")
    wheel("shared_dep", "1.0.0")
    wheel("shared_dep", "2.0.0")
    components = {kind: {"version_id": None} for kind in service.KINDS}
    for i, (kind, requirement, old_version) in enumerate((("model", ">=1,<3", "1.0.0"), ("optimize", ">=2,<3", "2.0.0"))):
        factor = wheel("factor_abcd", f"1.0.{i + 1}", ["scheme>=1,<2"])
        factor = factor.rename(tmp_path / factor.name)
        candidate = wheel(f"{kind}_demo", "1.0.0", [f"shared-dep{requirement}", "scheme>=1,<2",
                                                   f"factor-abcd{factor_requirements[i]}"])
        environment = tmp_path / kind
        environment.mkdir()
        (environment / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": {
            "factor-abcd": {"path": str(factor)}, "scheme": {"path": str(scheme)},
        }}}}))
        (environment / "uv.lock").write_text(f'[[package]]\nname="shared-dep"\nversion="{old_version}"\n')
        components[kind] = {
            "version_id": str(uuid4()), "scheme_version": "1.0.0",
            "input": {"environment": {"lockfile": str(environment / "uv.lock")}},
            "component": {"package": f"{kind}-demo", "version": "1.0.0", "wheel": str(candidate),
                          "sha256": hashlib.sha256(candidate.read_bytes()).hexdigest(), "entry": f"{kind}_demo:Algo"},
        }
    monkeypatch.setattr(service, "inspect_component", lambda *args: {"params": {}, "backtest": {}})
    run = service.command

    def offline(args, cwd, data=None):
        if args[:2] in (["uv", "lock"], ["uv", "sync"]):
            args = [*args, "--offline", "--find-links", str(wheels), "--python", sys.executable]
        return run(args, cwd, data)

    monkeypatch.setattr(service, "command", offline)
    destination = tmp_path / "assembled"
    if reverse:
        components = dict(reversed(list(components.items())))
    if expected is None:
        with pytest.raises(ValueError, match="No solution"):
            service.build(components, {kind: {} for kind in service.KINDS}, destination)
        assert not (destination / "input.json").exists()
        return
    assert service.build(components, {kind: {} for kind in service.KINDS}, destination) == "1.0.0"
    lock = tomllib.loads((destination / "environment/uv.lock").read_text())
    assert next(p["version"] for p in lock["package"] if p["name"] == "shared-dep") == "2.0.0"
    assert next(p["version"] for p in lock["package"] if p["name"] == "factor-abcd") == expected
    assert next(p["version"] for p in lock["package"] if p["name"] == "scheme") == "1.0.0"
    # 冻结结果在原始候选 wheel 消失后仍可安装。
    for factor in tmp_path.glob("factor_abcd-*.whl"):
        factor.unlink()
    service.command(["uv", "sync", "--locked", "--no-dev"], destination / "environment")
    assert json.loads((destination / "input.json").read_text())["kind"] == "strategy"
