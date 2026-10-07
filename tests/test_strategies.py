"""策略组装的版本边界、参数冲突与冻结输入。"""

import hashlib
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4
from zipfile import ZipFile

import pytest
import tomlkit
import tomllib
from core.apps.strategies import service
from fastapi import HTTPException


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
    (directory / "input.json").write_text(json.dumps({
        "kind": kind, "algos": {kind: {"version": version.package_version, "package": package,
             "wheel": str(wheel), "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
             "entry": f"{package.replace('-', '_')}:Algo"}},
        "environment": {"lockfile": str(directory / "uv.lock")},
    }))
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
