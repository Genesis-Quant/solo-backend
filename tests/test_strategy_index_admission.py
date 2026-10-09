"""Real offline uv + HTTP admission for run-local shared-transitive wheel indexes."""

import ast
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID, uuid4
from zipfile import ZipFile

import pytest
import tomlkit
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from config import SoloSettings
from core.apps import version_policy as policy
from core.apps.artifacts.models import ResearchArtifact
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies import service
from core.apps.strategies import views as strategy_views
from core.apps.strategies.models import Strategy
from core.database.base import Base
from core.database.session import get_database_session
from main import app


def make_wheel(directory, package, version, requirements=(), *, module=True):
    name = package.replace("-", "_")
    path = directory / f"{name}-{version}-py3-none-any.whl"
    metadata = f"{name}-{version}.dist-info"
    with ZipFile(path, "w") as archive:
        archive.writestr(f"{metadata}/METADATA", f"Metadata-Version: 2.1\nName: {package}\nVersion: {version}\n"
                         + "".join(f"Requires-Dist: {value}\n" for value in requirements))
        archive.writestr(f"{metadata}/WHEEL", "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{metadata}/RECORD", "")
        if module:
            archive.writestr(f"{name}/__init__.py", "")
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def indexed_strategy(tmp_path, monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    session = Session(engine, expire_on_commit=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path)
    monkeypatch.setattr(SoloSettings, "HOME_DIR", home)
    app.dependency_overrides[get_database_session] = lambda: session
    scheduler = MagicMock()
    monkeypatch.setattr(strategy_views, "DolphinSchedulerClient", scheduler)
    # Forms are not the finding under test; selected/build/policy and uv remain real.
    monkeypatch.setattr(service, "inspect_component", lambda *args: {"params": {}, "backtest": {}})

    def offline_uv(args, cwd, data=None):
        assert args[:2] in (["uv", "lock"], ["uv", "sync"])
        assert cwd.resolve().is_relative_to(tmp_path.resolve())
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home),
               "UV_PROJECT_ENVIRONMENT": str(cwd / ".venv"), "UV_PYTHON_INSTALL_DIR": str(home / ".python"),
               "UV_CACHE_DIR": str(tmp_path / "uv-cache"), "UV_NO_CACHE": "1", "UV_OFFLINE": "1",
               "UV_LINK_MODE": "copy", "PYTHONDONTWRITEBYTECODE": "1"}
        for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
            env.pop(key, None)
        result = subprocess.run([*args, "--offline", "--no-cache", "--python", sys.executable,
                                 "--no-python-downloads"], cwd=cwd, env=env, text=True,
                                capture_output=True, timeout=120, check=False)
        if result.returncode:
            raise ValueError((result.stderr or result.stdout)[-4000:])
        return result.stdout

    monkeypatch.setattr(service, "command", offline_uv)

    def project(kind):
        item = Project(id=uuid4(), name=str(uuid4()), kind=kind, template_tag="v1.2.0", template_commit="a" * 40,
                       template_package_version="1.2.0", scheme_version="v1.2.0", scheme_commit="b" * 40)
        session.add(item)
        session.commit()
        directory = policy.project_path(item)
        directory.mkdir(parents=True)
        (directory / ".solo").write_text(json.dumps({"project_id": str(item.id), "name": item.name,
                                                    "kind": item.kind, "package_name": item.package_name}))
        (directory / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
        return item

    def seed(client, factor_requirements=(">=1.2.1,<1.3.0", ">=1.2.2,<1.3.0"), schemes=("1.2.0", "1.2.7"), *, model_indexes=(), expected_error=None, publication=False):
        factor_root = project("factor")
        if publication:
            factor_root.template_commit = "c" * 40
            session.commit()
        factor_package = f"factor-{factor_root.id.hex[:4]}"
        selections, originals, factor_wheels = {}, [], []
        # Register each exact Factor patch from its own frozen run first. A
        # consumer's Scheme (including 1.2.7) is not Factor source provenance.
        for number in (1, 2):
            saved = ProjectVersion(id=uuid4(), project_id=factor_root.id, number=number,
                                   package_version=f"1.2.{number}", status="success", scheme_version="1.2.0",
                                   parameters={"factor": {}, "analysis": {}})
            session.add(saved)
            session.commit()
            run = policy.run_path(saved.id)
            environment = run / "environment"
            environment.mkdir(parents=True)
            factor = make_wheel(run, factor_package, saved.package_version, ["scheme>=1.2.0,<1.3.0"])
            actual_scheme = make_wheel(run, "scheme", "1.2.0")
            sources = {factor_package: {"path": str(factor)}, "scheme": {"path": str(actual_scheme)}}
            (environment / "pyproject.toml").write_text(tomlkit.dumps({
                "project": {"name": "factor-environment", "version": "0.0.0", "requires-python": ">=3.12,<3.13",
                            "dependencies": [f"{factor_package}=={saved.package_version}", "scheme==1.2.0"]},
                "tool": {"uv": {"package": False, "sources": sources}},
            }))
            lockfile = environment / "uv.lock"
            lockfile.write_text(tomlkit.dumps({"package": [
                {"name": name, "version": version, "source": {"path": str(path)}}
                for name, version, path in (("scheme", "1.2.0", actual_scheme),
                                            (factor_package, saved.package_version, factor))
            ]}))
            component = {"package": factor_package, "version": saved.package_version,
                         "entry": f"{factor_package.replace('-', '_')}:Factor", "wheel": str(factor),
                         "sha256": digest(factor), "params": {}}
            (run / "input.json").write_text(json.dumps({"kind": "factor", "environment": {"lockfile": str(lockfile)},
                                                       "factor": component, "analysis": {}}))
            saved.input_sha256, saved.lock_sha256 = digest(run / "input.json"), digest(lockfile)
            saved.accepted_artifacts = policy.capture_task_artifacts(session, saved, project=factor_root,
                                                                   kind="factor", directory=run)
            snapshot = saved.accepted_artifacts[factor_package]
            assert snapshot["scheme_version"] == snapshot["sources"]["scheme"]["version"] == "1.2.0"
            assert snapshot["origin_project_id"] == str(factor_root.id)
            assert snapshot["origin_version_id"] == str(saved.id)
            saved.finished_at = saved.created_at
            session.commit()
            factor_wheels.append(factor)
            originals.extend([factor, actual_scheme])
        for i, (kind, factor_requirement, scheme) in enumerate(zip(("model", "optimize"), factor_requirements, schemes, strict=True)):
            root = project(kind)
            package = f"{kind}-{root.id.hex[:4]}"
            saved = ProjectVersion(id=uuid4(), project_id=root.id, number=1, package_version=f"1.2.{i + 3}",
                                   status="success", scheme_version=scheme)
            session.add(saved)
            session.commit()
            run = policy.run_path(saved.id)
            run.mkdir(parents=True)
            factor = run / factor_wheels[i].name
            shutil.copyfile(factor_wheels[i], factor)
            actual_scheme = make_wheel(run, "scheme", scheme)
            candidate = make_wheel(run, package, saved.package_version,
                                   ["scheme>=1.2.0,<1.3.0", f"{factor_package}{factor_requirement}"])
            originals.extend([candidate, factor, actual_scheme])
            environment = run / "environment"
            environment.mkdir()
            sources = {factor_package: {"path": str(factor)}, "scheme": {"path": str(actual_scheme)}}
            indexes = [{"name": "old-flat", "url": "../old-wheels", "format": "flat"}]
            if kind == "model":
                indexes += [{**index, **({"name": index["name"].replace("{factor}", factor_package)} if "name" in index else {})}
                            for index in model_indexes]
            (environment / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": sources, "index": indexes}}}))
            lock = {"package": [{"name": name, "version": version, "source": {"path": str(path)}}
                                for name, version, path in (("scheme", scheme, actual_scheme),
                                (factor_package, f"1.2.{i + 1}", factor), (package, saved.package_version, candidate))]}
            lockfile = environment / "uv.lock"
            lockfile.write_text(tomlkit.dumps(lock))
            component = {"package": package, "version": saved.package_version, "entry": f"{package.replace('-', '_')}:{kind.title()}Algo",
                         "wheel": str(candidate), "sha256": digest(candidate), "params": {}}
            data = {"kind": kind, "environment": {"lockfile": str(lockfile)}, "algos": {kind: component}, "backtest": {}}
            (run / "input.json").write_text(json.dumps(data))
            if publication:
                config = tomllib.loads((environment / "pyproject.toml").read_text())
                config["project"] = {"name": "saved-environment", "version": "0.0.0", "requires-python": ">=3.12,<3.13",
                                     "dependencies": [f"{package}=={saved.package_version}", f"scheme=={scheme}"]}
                config["tool"]["uv"].update(package=False, sources={**sources, package: {"path": str(candidate)}},
                                           **{"constraint-dependencies": list(sources)})
                # The legacy fixture's unused flat registry is not a runtime source.
                config["tool"]["uv"]["index"] = [index for index in indexes if index.get("format") != "flat"]
                (environment / "pyproject.toml").write_text(tomlkit.dumps(config))
                lockfile.unlink()  # Replace the test-only skeletal lock before initial acceptance.
                offline_uv(["uv", "lock", "--project", str(environment)], environment)
            saved.accepted_artifacts = policy.capture_task_artifacts(session, saved, project=root, kind=kind, directory=run)
            saved.input_sha256, saved.lock_sha256 = digest(run / "input.json"), digest(lockfile)
            if publication:
                saved.finished_at = saved.created_at
                report = run / "report"
                report.mkdir()
                (report / "run.json").write_text(json.dumps({"status": "success", "input_sha256": saved.input_sha256,
                    "lock_sha256": saved.lock_sha256, "versions": {"scheme": scheme}}))
            session.commit()
            selections[kind] = str(saved.id)
        if publication:
            return selections, factor_root, originals
        response = client.post("/api/v1/strategies", json={
            "name": "shared transitive wheel", "selections": selections, "forms": {kind: {} for kind in service.KINDS},
        })
        assert response.status_code == 201, response.text
        if expected_error:
            assert response.json()["status"] == "failed", response.text
            assert expected_error in response.json()["error"]
            scheduler.return_value.__enter__.return_value.start.assert_not_called()
            return response
        assert response.json()["status"] == "queued", response.text
        strategy = session.get(Strategy, UUID(response.json()["id"]))
        scheduler.return_value.__enter__.return_value.start.assert_called_once()
        return strategy, factor_root, originals

    try:
        with TestClient(app) as client:
            yield client, session, tmp_path, seed, offline_uv
    finally:
        app.dependency_overrides.clear()
        session.close()
        engine.dispose()


def runtime_check(client, strategy):
    return client.post(f"/api/v1/version-policy/tasks/{strategy.id}/check", json={
        "kind": "strategy", "input_sha256": strategy.input_sha256, "lock_sha256": strategy.lock_sha256,
    })


def validate_actual_scheme_strategy(input_file):
    scheme = Path(__file__).resolve().parents[2] / "algos/scheme"
    source = scheme / "src/scheme/execute/schema.py"
    if not source.is_file():
        pytest.skip("Actual Scheme source checkout is required for strict wire validation")
    python = scheme / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if python.is_file():
        code = """
import json
import sys
import tomllib
from pathlib import Path
from pydantic import ValidationError
import scheme.execute.schema as schema
root = Path(sys.argv[1])
assert tomllib.loads((root / 'pyproject.toml').read_text())['project']['version'] == '1.2.2'
assert Path(schema.__file__).resolve() == (root / 'src/scheme/execute/schema.py').resolve()
StrategyTask = schema.StrategyTask
Package = StrategyTask.model_fields['algos'].annotation.model_fields['model'].annotation
assert set(Package.model_fields) == {'package', 'version', 'wheel', 'sha256', 'entry'}
assert Package.model_config['extra'] == 'forbid'
data = json.loads(Path(sys.argv[2]).read_text())
task = StrategyTask.model_validate(data)
assert task.backtest.start.isoformat() == '2026-09-10'
assert task.backtest.end.isoformat() == '2026-09-12'
for extra, value in [('artifact_id', '420d8068-6303-16b4-6d14-183cd4766330'), ('params', {})]:
    invalid = json.loads(json.dumps(data))
    invalid['algos']['model'][extra] = value
    try:
        StrategyTask.model_validate(invalid)
    except ValidationError as error:
        assert any(item['loc'] == ('algos', 'model', extra) and item['type'] == 'extra_forbidden'
                   for item in error.errors()), error
    else:
        raise AssertionError(f'{extra} must remain extra_forbidden')
print('Actual Scheme 1.2.2 StrategyTask validated; artifact_id and params remain extra_forbidden')
"""
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        result = subprocess.run([str(python), "-B", "-c", code, str(scheme), str(input_file)],
                                cwd=input_file.parent, env=env, text=True, capture_output=True,
                                timeout=30, check=False)
        assert result.returncode == 0, result.stdout + result.stderr
    else:
        # Backend-only environments still verify the exact actual SDK declaration,
        # not a hand-written mock of the Package contract.
        tree = ast.parse(source.read_text(encoding="utf-8"))
        models = {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}
        package = models["Package"]
        fields = {node.target.id for node in package.body if isinstance(node, ast.AnnAssign)}
        assert fields == {"package", "version", "wheel", "sha256", "entry"}
        algos = next(node for node in models["AlgoTask"].body
                     if isinstance(node, ast.AnnAssign) and node.target.id == "algos")
        assert ast.unparse(algos.annotation) == "AlgoComponents[Package]"
        assert any(isinstance(node, ast.Call) and any(key.arg == "extra" and isinstance(key.value, ast.Constant)
                   and key.value.value == "forbid" for key in node.keywords)
                   for node in ast.walk(models["StrictModel"]))
        data = json.loads(input_file.read_text())
        assert all(set(component) == fields for component in data["algos"].values())


@pytest.mark.parametrize("requirements,chosen", [
    ((">=1.2.1,<1.3.0", ">=1.2.2,<1.3.0"), "1.2.2"),
    ((">=1.2.1,<1.2.2", ">=1.2.1,<1.3.0"), "1.2.1"),
])
@pytest.mark.parametrize("schemes", [("1.2.0", "1.2.7"), ("1.2.7", "1.2.0")])
@pytest.mark.parametrize("model_indexes", [(), ({"name": "private", "url": "https://private.invalid/simple", "default": True},)])
def test_strategy_endpoint_admits_exact_flat_index_choice_and_installs_offline(indexed_strategy, requirements, chosen, schemes, model_indexes):
    client, _, _, seed, offline_uv = indexed_strategy
    strategy, factor_root, originals = seed(client, requirements, schemes, model_indexes=model_indexes)
    environment = policy.run_path(strategy.id) / "environment"
    lockfile = environment / "uv.lock"
    frozen_lock = lockfile.read_bytes()
    lock = tomllib.loads(frozen_lock.decode())
    package = f"factor-{factor_root.id.hex[:4]}"
    config = tomllib.loads((environment / "pyproject.toml").read_text())["tool"]["uv"]
    expected = list(model_indexes) or [{"url": "https://pypi.tuna.tsinghua.edu.cn/simple", "default": True}]
    assert config["index"] == expected + [
        {"name": f"wheels-{package}", "url": str(environment.parent / "wheels" / package), "format": "flat"},
    ]
    assert "old-wheels" not in (environment / "pyproject.toml").read_text()
    factor = next(item for item in lock["package"] if item["name"] == package)
    assert factor["version"] == chosen
    assert factor["source"]["registry"] == f"../wheels/{package}"
    wheel = policy.locked_local_wheel(policy.run_path(strategy.id), lockfile, factor)
    assert chosen in wheel.name
    assert len(list(wheel.parent.glob("*.whl"))) == 2
    assert strategy.scheme_version == schemes[0]
    admitted = runtime_check(client, strategy)
    assert admitted.status_code == 200, admitted.text
    assert admitted.json() == {"allowed": True, "grandfathered": False,
                               "artifacts": policy.accepted_runtime_artifacts(strategy, policy.run_path(strategy.id), "strategy")}
    # Original result wheels can disappear; runtime must install this run's exact
    # chosen artifact and Model Scheme, not another project or higher patch.
    for original in originals:
        original.unlink()
    offline_uv(["uv", "sync", "--locked", "--no-dev"], environment)
    assert lockfile.read_bytes() == frozen_lock
    python = environment / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = subprocess.run([str(python), "-B", "-c", f"import importlib.metadata as m; print(m.version('{package}')); print(m.version('scheme'))"],
                               cwd=environment, text=True, capture_output=True, timeout=30, check=True)
    assert installed.stdout.splitlines() == [chosen, schemes[0]]
    admitted = runtime_check(client, strategy)
    assert admitted.status_code == 200, admitted.text
    assert admitted.json() == {"allowed": True, "grandfathered": False,
                               "artifacts": policy.accepted_runtime_artifacts(strategy, policy.run_path(strategy.id), "strategy")}


@pytest.mark.parametrize("requirements,chosen", [
    ((">=1.2.1,<1.3.0", ">=1.2.2,<1.3.0"), "1.2.2"),
    ((">=1.2.1,<1.2.2", ">=1.2.1,<1.3.0"), "1.2.1"),
])
@pytest.mark.parametrize("schemes", [("1.2.0", "1.2.7"), ("1.2.7", "1.2.0"), ("1.2.2", "1.2.2")])
def test_published_artifact_strategy_after_origin_project_version_and_run_deleted(indexed_strategy, monkeypatch, requirements, chosen, schemes):
    from sqlalchemy import select
    from core.apps.artifacts import service as artifacts

    client, session, root, seed, offline_uv = indexed_strategy
    selections, factor_root, _ = seed(client, requirements, schemes, publication=True,
                                     model_indexes=({"name": "private", "url": "https://private.invalid/simple", "default": True},))
    artifacts_by_kind, original_hashes = {}, {}
    for kind, identifier in selections.items():
        saved = session.get(ProjectVersion, UUID(identifier))
        directory = policy.run_path(saved.id)
        original_hashes[kind] = (digest(directory / "input.json"), saved.input_sha256,
                                 digest(directory / "environment/uv.lock"), saved.lock_sha256)
        response = client.post(f"/api/v1/projects/{saved.project_id}/versions/{saved.id}/publish", json={})
        assert response.status_code == 200, response.text
        artifacts_by_kind[kind] = response.json()["artifactId"]
        assert response.json()["publishStatus"] == "published"
        assert (digest(directory / "input.json"), saved.input_sha256,
                digest(directory / "environment/uv.lock"), saved.lock_sha256) == original_hashes[kind]
    published = {kind: session.get(ResearchArtifact, UUID(identifier)) for kind, identifier in artifacts_by_kind.items()}
    for artifact in published.values():
        bundle = artifacts.verify_bundle(artifact)[0]
        assert not (bundle / "environment/.venv").exists()
    for project in list(session.scalars(select(Project))):
        for version in project.versions:
            shutil.rmtree(policy.run_path(version.id))
        shutil.rmtree(policy.project_path(project))
        session.delete(project)
    session.commit()
    artifacts.gc_unreferenced(session)
    session.commit()
    assert session.scalars(select(Project)).all() == []
    assert session.scalars(select(ProjectVersion)).all() == []
    assert list((root / "runs").iterdir()) == []
    for artifact in published.values():
        bundle, data = artifacts.verify_bundle(artifact)
        lockfile = bundle / "environment/uv.lock"
        frozen_lock = lockfile.read_bytes()
        offline_uv(["uv", "sync", "--project", str(lockfile.parent), "--locked", "--no-dev"], lockfile.parent)
        assert lockfile.read_bytes() == frozen_lock
        assert not str(root / "runs") in json.dumps(data)
        downloaded = client.get(f"/api/v1/artifacts/{artifact.id}/wheel")
        assert downloaded.status_code == 200, downloaded.text
        assert hashlib.sha256(downloaded.content).hexdigest() == artifact.sha256
    selected = service.selected(session, {"artifacts": artifacts_by_kind})
    original_selected = json.loads(json.dumps(selected))
    for kind, identifier in artifacts_by_kind.items():
        assert selected[kind]["component"]["artifact_id"] == identifier
        assert selected[kind]["component"]["params"] == {}
    # Only form inspection is outside this regression; artifact selection, build,
    # publication, source deletion, admission and offline uv are all real.
    monkeypatch.setattr(service, "inspect_component", lambda _, kind, values, *args: {
        "params": {}, "backtest": dict(values) if kind == "model" else {},
    })
    forms = {kind: {} for kind in service.KINDS}
    forms["model"] = {"start": "2026-09-10", "end": "2026-09-12"}
    response = client.post("/api/v1/strategies", json={
        "name": "published without originals", "selections": {"artifacts": artifacts_by_kind},
        "forms": forms,
    })
    assert response.status_code == 201, response.text
    assert response.json()["status"] == "queued", response.text
    strategy = session.get(Strategy, UUID(response.json()["id"]))
    for kind in artifacts_by_kind:
        descriptor = strategy.components[kind]
        assert descriptor["artifact_id"] == artifacts_by_kind[kind]
        assert descriptor["version_id"] is None
        assert descriptor["component_snapshot"]["sha256"] == published[kind].sha256
        assert descriptor["root_sources"] == published[kind].sources
        assert descriptor["accepted_artifacts"] == selected[kind]["accepted_artifacts"]
        assert strategy.accepted_artifacts[published[kind].package_name]["artifact_id"] == artifacts_by_kind[kind]
        assert not {"input", "component", "environment_path"} & set(descriptor)
    assert selected == original_selected
    input_file = policy.run_path(strategy.id) / "input.json"
    wire = json.loads(input_file.read_text())
    assert wire["backtest"] == forms["model"]
    for kind in artifacts_by_kind:
        assert set(wire["algos"][kind]) == {"package", "version", "wheel", "sha256", "entry"}
        assert wire["algos"][kind]["sha256"] == published[kind].sha256
    assert "artifact_id" not in json.dumps(wire)
    if schemes == ("1.2.2", "1.2.2"):
        validate_actual_scheme_strategy(input_file)
    environment = policy.run_path(strategy.id) / "environment"
    config = tomllib.loads((environment / "pyproject.toml").read_text())["tool"]["uv"]
    assert config["index"][0] == {"name": "private", "url": "https://private.invalid/simple", "default": True}
    factor_package = f"factor-{factor_root.id.hex[:4]}"
    lockfile = environment / "uv.lock"
    lock = tomllib.loads(lockfile.read_text())
    assert next(item["version"] for item in lock["package"] if item["name"] == factor_package) == chosen
    assert next(item["version"] for item in lock["package"] if item["name"] == "scheme") == schemes[0]
    assert strategy.accepted_artifacts[factor_package]["version"] == chosen
    assert runtime_check(client, strategy).status_code == 200
    offline_uv(["uv", "sync", "--locked", "--no-dev"], environment)
    assert runtime_check(client, strategy).status_code == 200
    original_descriptors = json.loads(json.dumps(strategy.components))
    changed = json.loads(json.dumps(strategy.components))
    changed["model"]["artifact_id"] = str(uuid4())
    strategy.components = changed
    session.commit()
    rejected = runtime_check(client, strategy)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["detail"]["code"] == "task_identity_mismatch"
    strategy.components = original_descriptors
    session.commit()
    assert runtime_check(client, strategy).status_code == 200
    # Retirement is evaluated from the frozen transitive source after its origin
    # has gone, before forms can sync or execute a published bundle.
    retirement = json.loads(json.dumps(policy.load_policy()))
    retirement["retired_releases"]["template"].append({
        "tag": "transitive-source-now-retired", "commit": "c" * 40, "reason": "frozen factor source retired",
    })
    monkeypatch.setattr(policy, "load_policy", lambda: retirement)
    commands = MagicMock(side_effect=AssertionError("retired artifacts must not execute"))
    monkeypatch.setattr(service, "command", commands)
    retired = client.post("/api/v1/strategies/forms", json={"artifacts": {"model": artifacts_by_kind["model"]}})
    assert retired.status_code == 422, retired.text
    assert retired.json()["detail"]["code"] == "version_retired"
    assert "frozen factor source retired" in retired.json()["detail"]["reason"]
    commands.assert_not_called()
    retired_task = runtime_check(client, strategy)
    assert retired_task.status_code == 422
    assert retired_task.json()["detail"]["code"] == "version_retired"


def test_strategy_frozen_acceptance_survives_all_origin_rows_and_resources_deleted(indexed_strategy):
    from sqlalchemy import select
    from core.apps.artifacts import service as artifacts

    client, session, root, seed, offline_uv = indexed_strategy
    strategy, factor_root, _ = seed(client)
    snapshots = json.loads(json.dumps(strategy.accepted_artifacts))
    descriptors = json.loads(json.dumps(strategy.components))
    input_file = policy.run_path(strategy.id) / "input.json"
    lockfile = policy.run_path(strategy.id) / "environment/uv.lock"
    frozen_input, frozen_lock = input_file.read_bytes(), lockfile.read_bytes()
    for version in list(session.scalars(select(ProjectVersion))):
        shutil.rmtree(policy.run_path(version.id))
        session.delete(version)
    session.flush()
    for project in list(session.scalars(select(Project))):
        shutil.rmtree(policy.project_path(project))
        session.delete(project)
    session.commit()
    artifacts.gc_unreferenced(session)
    session.commit()
    assert session.scalars(select(Project)).all() == []
    assert session.scalars(select(ProjectVersion)).all() == []
    assert runtime_check(client, strategy).status_code == 200
    offline_uv(["uv", "sync", "--locked", "--no-dev"], lockfile.parent)
    assert runtime_check(client, strategy).status_code == 200
    assert input_file.read_bytes() == frozen_input
    assert lockfile.read_bytes() == frozen_lock
    assert strategy.accepted_artifacts == snapshots
    assert strategy.components == descriptors
    assert f"factor-{factor_root.id.hex[:4]}" in snapshots
    assert session.get(Strategy, strategy.id) is strategy
    assert len(list((root / "runs").iterdir())) == 1


def test_strategy_runtime_uses_accepted_source_snapshot_not_mutable_origin_project(indexed_strategy):
    client, session, _, seed, _ = indexed_strategy
    strategy, factor_root, _ = seed(client)
    snapshots = json.loads(json.dumps(strategy.accepted_artifacts))
    factor_root.template_commit = policy.load_policy()["retired_releases"]["template"][0]["commit"]
    session.commit()
    assert runtime_check(client, strategy).status_code == 200
    assert strategy.accepted_artifacts == snapshots


def test_strategy_accepted_artifact_snapshots_are_immutable_after_queue(indexed_strategy):
    client, session, _, seed, _ = indexed_strategy
    strategy, _, _ = seed(client)
    original = json.loads(json.dumps(strategy.accepted_artifacts))
    changed = json.loads(json.dumps(original))
    package = next(name for name in changed if name.startswith("model-"))
    changed[package]["sha256"] = "0" * 64
    strategy.accepted_artifacts = changed
    with pytest.raises(ValueError, match="immutable|不可|冻结"):
        session.commit()
    session.rollback()
    session.refresh(strategy)
    assert strategy.accepted_artifacts == original
    assert runtime_check(client, strategy).status_code == 200


def test_strategy_rejects_registry_name_collision_with_generated_flat_index(indexed_strategy):
    client, session, _, seed, _ = indexed_strategy
    response = seed(client, model_indexes=({"name": "wheels-{factor}", "url": "https://private.invalid/simple", "explicit": True},),
                    expected_error="名称冲突")
    strategy = session.get(Strategy, UUID(response.json()["id"]))
    assert not (policy.run_path(strategy.id) / "input.json").exists()


@pytest.mark.parametrize("location", ["path", "relative-url", "file-url"])
def test_lock_registry_exact_location_and_sha256_are_binding(tmp_path, location):
    run = tmp_path / "run"
    environment = run / "environment"
    registry = run / "wheels/factor-abcd"
    registry.mkdir(parents=True)
    environment.mkdir()
    selected = make_wheel(registry, "factor-abcd", "1.2.1", ["scheme>=1.2.0,<1.3.0"])
    make_wheel(registry, "factor-abcd", "1.2.2", ["scheme>=1.2.0,<1.3.0"])
    wheel = {"hash": f"sha256:{digest(selected)}"}
    if location == "path":
        wheel["path"] = selected.name
    elif location == "relative-url":
        wheel["url"] = f"../wheels/factor-abcd/{selected.name}"
    else:
        wheel["url"] = selected.as_uri()
    package = {"name": "factor-abcd", "version": "1.2.1", "source": {"registry": "../wheels/factor-abcd"}, "wheels": [wheel]}
    assert policy.locked_local_wheel(run, environment / "uv.lock", package) == selected.resolve()
    wheel["hash"] = "sha256:" + "0" * 64
    with pytest.raises(ValueError, match="sha256"):
        policy.locked_local_wheel(run, environment / "uv.lock", package)
    selected.unlink()
    with pytest.raises(ValueError, match="本地产物"):
        policy.locked_local_wheel(run, environment / "uv.lock", package)


@pytest.mark.parametrize("tamper", ["remote-registry", "outside-registry", "outside-wheel", "missing-location",
                                     "ambiguous-wheels", "wrong-hash", "missing-choice", "metadata", "scheme-range",
                                     "entry-module", "component-source", "retired-root"])
def test_strategy_runtime_rejects_unbound_or_invalid_flat_index_artifacts(indexed_strategy, monkeypatch, tamper):
    client, session, root, seed, _ = indexed_strategy
    strategy, factor_root, _ = seed(client)
    run = policy.run_path(strategy.id)
    lockfile = run / "environment/uv.lock"
    lock = tomllib.loads(lockfile.read_text())
    name = f"factor-{factor_root.id.hex[:4]}"
    factor = next(item for item in lock["package"] if item["name"] == name)
    selected = policy.locked_local_wheel(run, lockfile, factor)
    if tamper == "remote-registry":
        factor["source"] = {"registry": "https://example.invalid/simple"}
    elif tamper == "outside-registry":
        outside = root / "outside-index"
        outside.mkdir()
        shutil.copy2(selected, outside / selected.name)
        factor["source"] = {"registry": str(outside)}
    elif tamper == "outside-wheel":
        factor["wheels"] = [{"url": str(next((root / "runs").glob(f"*/{selected.name}")))}]
    elif tamper == "missing-location":
        factor.pop("wheels")
    elif tamper == "ambiguous-wheels":
        factor["wheels"] = [{"path": path.name} for path in selected.parent.glob("*.whl")]
    elif tamper == "wrong-hash":
        factor["wheels"][0]["hash"] = "sha256:" + "0" * 64
    elif tamper == "missing-choice":
        selected.unlink()
        assert len(list(selected.parent.glob("*.whl"))) == 1
    elif tamper in {"metadata", "scheme-range", "entry-module"}:
        replacement = make_wheel(selected.parent, name, "1.2.99" if tamper == "metadata" else factor["version"],
                                 ["scheme>=1.2.0,<2.0.0" if tamper == "scheme-range" else "scheme>=1.2.0,<1.3.0"],
                                 module=tamper != "entry-module")
        if replacement != selected:
            selected.write_bytes(replacement.read_bytes())
            replacement.unlink()
    elif tamper == "component-source":
        model = next(item for item in lock["package"] if item["name"].startswith("model-"))
        original = policy.locked_local_wheel(run, lockfile, model)
        replacement = original.with_name("different-source.whl")
        shutil.copy2(original, replacement)
        model["source"] = {"path": str(replacement)}
    else:
        retirement = json.loads(json.dumps(policy.load_policy()))
        retirement["retired_releases"]["template"].append({
            "tag": "frozen-source-now-retired", "commit": strategy.accepted_artifacts[name]["sources"]["template"]["commit"],
            "reason": "frozen source retired after acceptance",
        })
        monkeypatch.setattr(policy, "load_policy", lambda: retirement)
    # Even a persisted envelope cannot authorize a remote/escaped/fallback wheel.
    lockfile.write_text(tomlkit.dumps(lock))
    strategy.lock_sha256 = digest(lockfile)
    session.commit()
    response = runtime_check(client, strategy)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == ("version_retired" if tamper == "retired-root" else "task_identity_mismatch")
