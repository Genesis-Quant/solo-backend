"""Local indexes may contain multiple candidates and repeated lock package entries."""

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from textwrap import dedent
from unittest.mock import MagicMock
from uuid import UUID
from zipfile import ZipFile

import pydantic
import pytest
import tomlkit

from core.apps import version_policy as policy
from core.apps.artifacts.service import artifact_path, artifact_snapshot, publish_version, register_file
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies import service, views
from core.apps.strategies.models import Strategy
from test_artifacts import make_project, make_run, make_wheel
from test_artifacts import registry as registry


@pytest.mark.parametrize("location", ["path", "relative-url", "file-url"])
def test_local_registry_selects_metadata_version_from_multiple_wheels(tmp_path, location):
    run = tmp_path / "run"
    environment = run / "environment"
    registry = run / "wheels/factor-abcd"
    registry.mkdir(parents=True)
    environment.mkdir()
    first = make_wheel(registry / "factor_abcd-1.2.1-py3-none-any.whl", "factor-abcd", "1.2.1")
    chosen = make_wheel(registry / "factor_abcd-1.2.2-py3-none-any.whl", "factor-abcd", "1.2.2")
    def entry(path):
        if location == "path":
            return {"path": path.name}
        return {"url": "../wheels/factor-abcd/" + path.name if location == "relative-url" else path.as_uri()}
    package = {"name": "factor-abcd", "version": "1.2.2", "source": {"registry": "../wheels/factor-abcd"},
               "wheels": [entry(first), entry(chosen)]}
    assert policy.locked_local_wheel(run, environment / "uv.lock", package) == chosen.resolve()
    assert policy.local_task_packages({"package": [package, package]}) == {"factor-abcd": package}


def test_local_registry_cannot_escape_its_run(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    outside = make_wheel(tmp_path / "outside/factor.whl", "factor-abcd")
    package = {"name": "factor-abcd", "version": "1.2.1", "source": {"registry": str(outside.parent)},
               "wheels": [{"path": outside.name}]}
    with pytest.raises(ValueError, match="registry"):
        policy.locked_local_wheel(run, run / "uv.lock", package)


def test_duplicated_scheme_entries_with_same_actual_source_are_allowed():
    package = {"name": "scheme", "version": "1.2.0", "source": {"git": "https://example.invalid/scheme#" + "b" * 40}}
    actual, commit = policy.locked_scheme({"package": [package, package]})
    assert str(actual) == "1.2.0" and commit == "b" * 40


def test_lock_file_keeps_external_tool_metadata_without_backend_digest_operations(tmp_path):
    package = {"name": "helper", "version": "1.0.0", "source": {"registry": "https://example.invalid/simple"},
               "wheels": [{"url": "https://example.invalid/helper.whl", "size": 12}]}
    path = Path(tmp_path) / "uv.lock"
    path.write_text(tomlkit.dumps({"package": [package, package]}))
    assert policy.local_task_packages(tomllib.loads(path.read_text())) == {}


@pytest.mark.parametrize("published", [False, True])
@pytest.mark.parametrize("chosen,reverse", [("1.2.1", False), ("1.2.1", True), ("1.2.2", False), ("1.2.2", True),
                                             ("shared", False), ("shared", True)])
def test_strategy_resolves_multiple_accepted_candidates_offline_and_keeps_uuid(
    registry, monkeypatch, chosen, reverse, published,
):
    session, root, client = registry
    shared = chosen == "shared"
    chosen = "1.2.1" if shared else chosen
    producer = make_project(session, root, kind="factor")
    artifacts = {}
    for version in ("1.2.1", "1.2.2"):
        wheel = make_wheel(policy.project_path(producer) / f"{producer.package_name.replace('-', '_')}-{version}-py3-none-any.whl",
                           producer.package_name, version)
        artifacts[version] = register_file(session, wheel, producer, scheme_version="1.2.0")
    model = make_project(session, root, kind="model")
    optimize = make_project(session, root, kind="optimize")
    components = {}
    for project, candidate_version in ((model, "1.2.1"), (optimize, "1.2.2")):
        artifact = artifacts[chosen if shared else candidate_version]
        project.artifact_dependencies = {artifact.package_name: artifact_snapshot(artifact)}
        session.commit()
        version, directory, component = make_run(session, root, project,
            assets={artifact.package_name: artifact_path(artifact)}, accepted=False)
        make_wheel(Path(component["wheel"]), project.package_name, version.package_version,
                   dependencies=[f"{producer.package_name}=={chosen}"])
        lockfile = directory / "environment/uv.lock"
        lock = tomllib.loads(lockfile.read_text())
        scheme = make_wheel(directory / "wheels/scheme-1.2.0-py3-none-any.whl", "scheme", "1.2.0", requires_scheme=False)
        lock["package"][0]["source"] = {"path": str(scheme)}
        lock["package"][0]["wheels"] = [{"filename": scheme.name}]
        lockfile.write_text(tomlkit.dumps(lock))
        configfile = lockfile.with_name("pyproject.toml")
        config = tomllib.loads(configfile.read_text())
        config["tool"]["uv"]["sources"]["scheme"] = {"path": str(scheme)}
        config["tool"]["uv"]["index"] = []
        configfile.write_text(tomlkit.dumps(config))
        version.accepted_artifacts = policy.capture_task_artifacts(session, version, project=project, kind=project.kind)
        session.commit()
        if published:
            artifact = publish_version(session, project, version)
            session.commit()
            components[project.kind] = str(artifact.id)
        else:
            components[project.kind] = str(version.id)
    if reverse:
        components = dict(reversed(list(components.items())))
    selection = {"artifacts": components} if published else components
    scheduler = MagicMock()
    monkeypatch.setattr(views, "DolphinSchedulerClient", scheduler)
    inspect = MagicMock(return_value={"params": {}, "backtest": {}})
    monkeypatch.setattr(service.ComponentInspector, "inspect", inspect)
    def offline(args, cwd, data=None):
        assert args[:2] in (["uv", "lock"], ["uv", "sync"])
        assert cwd.resolve().is_relative_to(root.resolve())
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
               "UV_PROJECT_ENVIRONMENT": str(cwd / ".venv"),
               "UV_CACHE_DIR": str(root / "uv-cache"), "UV_LINK_MODE": "copy"}
        for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
        result = subprocess.run([*args, "--offline", "--no-cache", "--python", sys.executable,
                                 "--no-python-downloads"], cwd=cwd, env=env, text=True, capture_output=True,
                                timeout=120, check=False)
        if result.returncode:
            raise ValueError(result.stderr or result.stdout)
        return result.stdout
    monkeypatch.setattr(service, "command", offline)
    response = views.create_strategy(views.CreateStrategy(name="multi-candidate", selections=selection,
        forms={kind: {} for kind in service.KINDS}), session)
    assert response["status"] == "queued", response
    strategy = session.get(Strategy, UUID(response["id"]))
    assert strategy.accepted_artifacts[producer.package_name]["id"] == str(artifacts[chosen].id)
    run = policy.run_path(strategy.id)
    local_wheels = run / "wheels" / producer.package_name
    assert len(list(local_wheels.glob("*.whl"))) == (1 if shared else 2)
    lockfile = run / "environment/uv.lock"
    lock = tomllib.loads(lockfile.read_text())
    locked = next(item for item in lock["package"] if item["name"] == producer.package_name)
    assert locked["version"] == chosen
    assert chosen in policy.locked_local_wheel(run, lockfile, locked).name
    data = json.loads((run / "input.json").read_text())
    assert all(set(item) == {"package", "version", "wheel", "entry"} for item in data["algos"].values())
    assert inspect.call_count == 4
    scheduler.return_value.__enter__.return_value.start.assert_called_once_with("strategy", str(run / "input.json"))
    from sqlalchemy import select
    for saved in list(session.scalars(select(ProjectVersion))):
        shutil.rmtree(policy.run_path(saved.id))
        session.delete(saved)
    for project in list(session.scalars(select(Project))):
        shutil.rmtree(policy.project_path(project))
        session.delete(project)
    session.commit()
    assert client.post(f"/api/v1/version-policy/tasks/{strategy.id}/check", json={"kind": "strategy"}).json() == {"allowed": True}
    offline(["uv", "sync", "--locked", "--no-dev"], lockfile.parent)
    assert strategy.accepted_artifacts[producer.package_name]["id"] == str(artifacts[chosen].id)


@pytest.mark.parametrize("kind", ["factor", "model"])
def test_published_bundle_locked_environment_installs_registry_paths_after_source_removal(registry, kind):
    session, root, _ = registry
    project = make_project(session, root, kind=kind)
    version, directory, component = make_run(session, root, project, accepted=False)
    helper = make_wheel(directory / "wheels/custom_helper-7.0.0-py3-none-any.whl", "custom-helper", "7.0.0", requires_scheme=False)
    with ZipFile(helper, "a") as archive:
        archive.writestr("custom_helper/version.py", 'VALUE = "7.0.0"\n')
    make_wheel(Path(component["wheel"]), project.package_name, version.package_version, dependencies=["custom-helper==7.0.0"])
    scheme = make_wheel(directory / "wheels/scheme-1.2.0-py3-none-any.whl", "scheme", "1.2.0", requires_scheme=False)
    environment = directory / "environment"
    config = tomllib.loads((environment / "pyproject.toml").read_text())
    config["project"]["requires-python"] = f"=={sys.version_info.major}.{sys.version_info.minor}.*"
    config["project"]["dependencies"].extend(["custom-helper==7.0.0", "scheme==1.2.0"])
    config["tool"]["uv"]["sources"].update(scheme={"path": str(scheme)}, **{"custom-helper": {"path": str(helper)}})
    config["tool"]["uv"]["index"] = []
    (environment / "pyproject.toml").write_text(tomlkit.dumps(config))
    (environment / "uv.lock").unlink()

    def offline(args, cwd, data=None):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1",
               "UV_PROJECT_ENVIRONMENT": str(cwd / ".venv"), "UV_CACHE_DIR": str(root / "uv-cache"), "UV_LINK_MODE": "copy"}
        for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
        result = subprocess.run([*args, "--offline", "--no-cache", "--python", sys.executable, "--no-python-downloads"],
                                cwd=cwd, env=env, text=True, capture_output=True, timeout=120, check=False)
        assert result.returncode == 0, result.stderr or result.stdout
        return result.stdout

    offline(["uv", "lock", "--project", str(environment)], environment)
    version.accepted_artifacts = policy.capture_task_artifacts(session, version, project=project, kind=kind)
    session.commit()
    artifact = publish_version(session, project, version)
    session.commit()
    bundle = Path(artifact.bundle_path)
    expected = {name: artifact_path(session.get(type(artifact), UUID(snapshot["id"])))
                for name, snapshot in version.accepted_artifacts.items()}
    assert not (bundle / "wheels").exists()
    assert not (bundle / "original").exists()
    config = tomllib.loads((bundle / "environment/pyproject.toml").read_text())
    assert config["tool"]["uv"]["sources"] == {name: {"path": str(path)} for name, path in expected.items()}
    session.delete(version)
    session.delete(project)
    session.commit()
    shutil.rmtree(directory)
    offline(["uv", "sync", "--project", str(bundle / "environment"), "--locked", "--no-dev"], bundle / "environment")
    python = bundle / "environment/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    result = subprocess.run([str(python), "-B", "-c", "from custom_helper.version import VALUE; "
                             f"import {artifact.package_name.replace('-', '_')}; print(VALUE)"],
                            cwd=bundle, capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "7.0.0"


def test_published_model_parameters_and_strategy_build_use_bundle_after_source_removal(registry, monkeypatch):
    session, root, _ = registry
    project = make_project(session, root)
    version, directory, component = make_run(session, root, project, accepted=False)
    package = project.package_name.replace("-", "_")
    with ZipFile(component["wheel"], "a") as archive:
        archive.writestr(f"{package}/forms.py", dedent("""\
            from pydantic import BaseModel, Field

            class ModelParams(BaseModel):
                window: int = Field(default=20, gt=0)

            class ModelAnalysis(ModelParams):
                cash: float

            class ModelReportForm(ModelParams):
                cash: float = 1000

                def build(self):
                    return ModelAnalysis(**self.model_dump())

            class ModelAlgo:
                params = ModelParams
        """))
    data = json.loads((directory / "input.json").read_text())
    data["algos"]["model"]["entry"] = package + ".forms:ModelAlgo"
    data["backtest"] = {"window": 17, "cash": 2500}
    (directory / "input.json").write_text(json.dumps(data))
    version.accepted_artifacts = policy.capture_task_artifacts(session, version, project=project, kind="model")
    session.commit()
    artifact = publish_version(session, project, version)
    session.commit()
    session.delete(version)
    session.delete(project)
    session.commit()
    shutil.rmtree(directory)
    shutil.rmtree(policy.project_path(project))
    components = service.selected(session, {"artifacts": {"model": str(artifact.id)}})
    environment = Path(artifact.bundle_path) / "environment"
    assert components["model"]["environment_path"] == str(environment)
    syncs = []
    python = environment / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    bootstrap = dedent("""\
        import runpy
        import sys
        from types import ModuleType
        from pydantic import BaseModel

        for name in ("scheme", "scheme.execute", "scheme.execute.strategy", "scheme.base",
                     "scheme.base.projects", "scheme.base.projects.optimize", "scheme.base.projects.control",
                     "scheme.base.projects.execution"):
            module = ModuleType(name)
            module.__path__ = []
            sys.modules[name] = module

        class EmptyParams(BaseModel):
            pass

        assembly = ModuleType("scheme.execute.strategy.assembly")
        assembly.parameter_type = lambda cls: cls.params
        sys.modules[assembly.__name__] = assembly
        for kind, name in (("optimize", "RiskParity"), ("control", "NoControl"), ("execution", "DirectExecution")):
            module = ModuleType("scheme.base.projects." + kind + ".default")
            setattr(module, name, type(name, (), {"params": EmptyParams}))
            sys.modules[module.__name__] = module
        runpy.run_path(sys.argv[1], run_name="__main__")
    """)

    def command(args, cwd, data=None):
        if args[:2] == ["uv", "sync"]:
            assert args == ["uv", "sync", "--project", str(environment), "--locked", "--no-dev"]
            sources = tomllib.loads((cwd / "pyproject.toml").read_text())["tool"]["uv"]["sources"]
            assert Path(sources[artifact.package_name]["path"]) == artifact_path(artifact)
            syncs.append(cwd)
            python.parent.mkdir(parents=True, exist_ok=True)
            wheel = artifact_path(artifact)
            with ZipFile(wheel) as archive:
                for name in archive.namelist():
                    if name.startswith(package + "/"):
                        archive.extract(name, cwd)
            return ""
        if args[:2] == ["uv", "lock"]:
            (cwd / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
            return ""
        assert args == [str(python), str(Path(service.__file__).with_name("parameters.py"))]
        # Execute the real parameter script and published module with a minimal
        # Scheme contract; only environment installation and Scheme are stubbed.
        env = {**os.environ, "PYTHONPATH": str(Path(pydantic.__file__).parent.parent),
               "PYTHONDONTWRITEBYTECODE": "1", "PYTHON_DOTENV_DISABLED": "1"}
        result = subprocess.run([sys.executable, "-B", "-c", bootstrap, args[1]], cwd=cwd, env=env,
                                input=json.dumps(data), text=True, capture_output=True, timeout=30, check=False)
        assert result.returncode == 0, result.stderr
        return result.stdout

    monkeypatch.setattr(service, "command", command)
    inspector = service.ComponentInspector(components)
    definition = inspector.inspect("model")
    assert definition["values"] == {"window": 17, "cash": 2500}
    assert definition["schema"]["properties"]["window"]["default"] == 20
    assert inspector.inspect("model", {"window": 23, "cash": 5000}) == {
        "backtest": {"window": 23, "cash": 5000}, "params": {"window": 23}, "common": {"cash": 5000},
    }
    assert syncs == [environment]
    destination = root / "runs/assembled-strategy"
    assert service.build(components, {kind: {"window": 23, "cash": 5000} if kind == "model" else {}
                                      for kind in service.KINDS}, destination) == "1.2.0"
    assert syncs == [environment, environment]
    assembled = json.loads((destination / "input.json").read_text())
    assert assembled["backtest"] == {"window": 23, "cash": 5000}
    assert Path(assembled["algos"]["model"]["wheel"]).is_file()
    assert assembled["algos"]["model"]["entry"] == package + ".forms:ModelAlgo"
