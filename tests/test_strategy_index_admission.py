"""Real offline uv + HTTP admission for run-local shared-transitive wheel indexes."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
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
        (directory / "uv.lock").write_text('[[package]]\nname="scheme"\nversion="1.2.0"\n')
        return item

    def seed(client, factor_requirements=(">=1.2.1,<1.3.0", ">=1.2.2,<1.3.0"), schemes=("1.2.0", "1.2.7")):
        factor_root = project("factor")
        factor_package = f"factor-{factor_root.id.hex[:4]}"
        selections, originals = {}, []
        for i, (kind, factor_requirement, scheme) in enumerate(zip(("model", "optimize"), factor_requirements, schemes, strict=True)):
            root = project(kind)
            package = f"{kind}-{root.id.hex[:4]}"
            saved = ProjectVersion(id=uuid4(), project_id=root.id, number=1, package_version=f"1.2.{i + 3}",
                                   status="success", scheme_version=scheme)
            session.add(saved)
            session.commit()
            run = policy.run_path(saved.id)
            run.mkdir(parents=True)
            factor = make_wheel(run, factor_package, f"1.2.{i + 1}", ["scheme>=1.2.0,<1.3.0"])
            actual_scheme = make_wheel(run, "scheme", scheme)
            candidate = make_wheel(run, package, saved.package_version,
                                   ["scheme>=1.2.0,<1.3.0", f"{factor_package}{factor_requirement}"])
            originals.extend([candidate, factor, actual_scheme])
            environment = run / "environment"
            environment.mkdir()
            sources = {factor_package: {"path": str(factor)}, "scheme": {"path": str(actual_scheme)}}
            (environment / "pyproject.toml").write_text(tomlkit.dumps({"tool": {"uv": {"sources": sources}}}))
            lock = {"package": [{"name": name, "version": version, "source": {"path": str(path)}}
                                for name, version, path in (("scheme", scheme, actual_scheme),
                                (factor_package, f"1.2.{i + 1}", factor), (package, saved.package_version, candidate))]}
            lockfile = environment / "uv.lock"
            lockfile.write_text(tomlkit.dumps(lock))
            component = {"package": package, "version": saved.package_version, "entry": f"{package.replace('-', '_')}:{kind.title()}Algo",
                         "wheel": str(candidate), "sha256": digest(candidate), "params": {}}
            data = {"kind": kind, "environment": {"lockfile": str(lockfile)}, "algos": {kind: component}, "backtest": {}}
            (run / "input.json").write_text(json.dumps(data))
            saved.input_sha256, saved.lock_sha256 = digest(run / "input.json"), digest(lockfile)
            session.commit()
            selections[kind] = str(saved.id)
        response = client.post("/api/v1/strategies", json={
            "name": "shared transitive wheel", "selections": selections, "forms": {kind: {} for kind in service.KINDS},
        })
        assert response.status_code == 201, response.text
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


@pytest.mark.parametrize("requirements,chosen", [
    ((">=1.2.1,<1.3.0", ">=1.2.2,<1.3.0"), "1.2.2"),
    ((">=1.2.1,<1.2.2", ">=1.2.1,<1.3.0"), "1.2.1"),
])
@pytest.mark.parametrize("schemes", [("1.2.0", "1.2.7"), ("1.2.7", "1.2.0")])
def test_strategy_endpoint_admits_exact_flat_index_choice_and_installs_offline(indexed_strategy, requirements, chosen, schemes):
    client, _, _, seed, offline_uv = indexed_strategy
    strategy, factor_root, originals = seed(client, requirements, schemes)
    environment = policy.run_path(strategy.id) / "environment"
    lockfile = environment / "uv.lock"
    frozen_lock = lockfile.read_bytes()
    lock = tomllib.loads(frozen_lock.decode())
    package = f"factor-{factor_root.id.hex[:4]}"
    factor = next(item for item in lock["package"] if item["name"] == package)
    assert factor["version"] == chosen
    assert factor["source"]["registry"] == f"../wheels/{package}"
    wheel = policy.locked_local_wheel(policy.run_path(strategy.id), lockfile, factor)
    assert chosen in wheel.name
    assert len(list(wheel.parent.glob("*.whl"))) == 2
    assert strategy.scheme_version == schemes[0]
    assert runtime_check(client, strategy).json() == {"allowed": True, "grandfathered": False}
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
    assert runtime_check(client, strategy).json() == {"allowed": True, "grandfathered": False}


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
def test_strategy_runtime_rejects_unbound_or_invalid_flat_index_artifacts(indexed_strategy, tamper):
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
        factor_root.template_commit = policy.load_policy()["retired_releases"]["template"][0]["commit"]
    # Even a persisted envelope cannot authorize a remote/escaped/fallback wheel.
    lockfile.write_text(tomlkit.dumps(lock))
    strategy.lock_sha256 = digest(lockfile)
    session.commit()
    response = runtime_check(client, strategy)
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == ("version_retired" if tamper == "retired-root" else "task_identity_mismatch")
