import json
import os
import subprocess
from pathlib import Path

import pytest

from config import SoloSettings
from core.apps.projects import environment
from core.apps.strategies import service
from core.utils.uv import uv_environment


@pytest.fixture
def uv_settings(tmp_path, monkeypatch):
    shared, home = tmp_path / "shared", tmp_path / "home"
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", shared)
    monkeypatch.setattr(SoloSettings, "HOME_DIR", home)
    monkeypatch.setenv("UV_LINK_MODE", "copy")
    monkeypatch.setenv("UV_CACHE_DIR", str(tmp_path / "old-cache"))
    monkeypatch.setenv("UV_INDEX_URL", "https://mirror.example/simple")
    monkeypatch.setenv("UV_EXTRA_INDEX_URL", "https://extra.example/simple")
    return shared, home


def assert_uv_settings(env, directory, cache, home):
    assert env["UV_LINK_MODE"] == "hardlink"
    assert env["UV_CACHE_DIR"] == str(cache)
    assert Path(env["UV_CACHE_DIR"]).is_absolute()
    assert env["UV_PROJECT_ENVIRONMENT"] == str(directory / ".venv")
    assert env["UV_PYTHON_INSTALL_DIR"] == str(home / ".python")
    assert env["UV_INDEX_URL"] == "https://mirror.example/simple"
    assert env["UV_EXTRA_INDEX_URL"] == "https://extra.example/simple"
    assert not cache.exists()


@pytest.mark.parametrize("path,group", [
    ("projects/model/example", "projects"),
    ("runs/run-id/environment", "runs"),
    ("artifacts/sha256/bundle/environment", "artifacts"),
    ("projects", "projects"),
    ("projects-other/example", None),
    ("runs-other/environment", None),
    ("artifacts-other/environment", None),
    ("../outside/environment", None),
])
def test_cache_uses_volume_root_or_nonshared_parent(uv_settings, path, group):
    shared, _ = uv_settings
    directory = shared / path
    absolute = Path(os.path.abspath(directory))
    cache = shared / group / ".uv-cache" if group else absolute.parent / ".uv-cache"
    assert uv_environment(directory) == {"UV_LINK_MODE": "hardlink", "UV_CACHE_DIR": str(cache)}
    assert not cache.exists()


def test_relative_paths_are_normalized_lexically(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", Path("shared/../shared"))
    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", lambda *args, **kwargs: pytest.fail("must not resolve symlinks"))
        settings = uv_environment(Path("shared/projects/../runs/run-id/environment"))
    assert settings == {
        "UV_LINK_MODE": "hardlink",
        "UV_CACHE_DIR": str(tmp_path / "shared/runs/.uv-cache"),
    }
    assert uv_environment(Path("outside/environment"))["UV_CACHE_DIR"] == str(tmp_path / "outside/.uv-cache")


@pytest.mark.parametrize("shared_project", [True, False])
@pytest.mark.parametrize("locked", [True, False])
def test_project_sync_passes_hardlink_environment(uv_settings, monkeypatch, shared_project, locked):
    shared, home = uv_settings
    directory = shared / "projects/model/example" if shared_project else shared.parent / "outside/example"
    cache = shared / "projects/.uv-cache" if shared_project else directory.parent / ".uv-cache"
    directory.mkdir(parents=True)
    notebook = directory / "research.ipynb"
    notebook.write_text('{"metadata": {}}', encoding="utf-8")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(environment.subprocess, "run", run)
    environment.prepare_environment(directory, "project-id", "Example", locked=locked)

    args, kwargs = calls[0]
    assert args == ["uv", "sync", "--project", str(directory), "--python", "3.12", "--managed-python",
                    *(["--locked"] if locked else [])]
    assert_uv_settings(kwargs["env"], directory, cache, home)
    assert kwargs["env"]["UV_PYTHON_BIN_DIR"] == "/tmp/solo-python-bin"
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["timeout"] == 600
    assert len(calls) == 2
    assert calls[1][0] == [str(directory / ".venv/bin/python"), "-c", "import ipykernel"]
    kernel = json.loads((environment.kernel_directory("project-id") / "kernel.json").read_text(encoding="utf-8"))
    assert kernel["argv"][0] == str(directory / ".venv/bin/python")
    assert json.loads(notebook.read_text(encoding="utf-8"))["metadata"]["kernelspec"]["name"] == "solo-project-id"
    assert not (directory / ".venv").exists()


@pytest.mark.parametrize("path,group", [
    ("projects/model/example", "projects"),
    ("runs/strategy-id/environment", "runs"),
    ("artifacts/sha256/bundle/environment", "artifacts"),
    ("../outside/environment", None),
])
@pytest.mark.parametrize("operation", ["sync", "lock"])
def test_strategy_command_passes_hardlink_environment(uv_settings, monkeypatch, path, group, operation):
    shared, home = uv_settings
    directory = (shared / path).absolute()
    cache = shared / group / ".uv-cache" if group else Path(os.path.abspath(directory)).parent / ".uv-cache"
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        monkeypatch.setenv(key, "inherited-value")
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "output", "")

    monkeypatch.setattr(service.subprocess, "run", run)
    args = ["uv", operation, "--project", str(directory)]
    data = {"kind": "model"}
    assert service.command(args, directory, data) == "output"
    captured_args, kwargs = calls[0]
    assert captured_args == args and kwargs["cwd"] == directory
    assert_uv_settings(kwargs["env"], directory, cache, home)
    assert all(key not in kwargs["env"] for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"))
    assert json.loads(kwargs["input"]) == data
    assert kwargs["capture_output"] is True and kwargs["text"] is True and kwargs["timeout"] == 600


@pytest.mark.parametrize("path,group", [
    ("runs/version-id/environment", "runs"),
    ("artifacts/sha256/bundle/environment", "artifacts"),
])
def test_component_inspection_uses_its_frozen_environment_volume(uv_settings, monkeypatch, path, group):
    shared, home = uv_settings
    directory = shared / path
    components = {"model": {
        "component": {"entry": "model_example:Algo"},
        "input": {"environment": {"lockfile": str(directory / "uv.lock")}, "backtest": {}},
    }}
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, '{"params": {}}', "")

    monkeypatch.setattr(service, "require_components", lambda components: None)
    monkeypatch.setattr(service.subprocess, "run", run)
    assert service.inspect_component(components, "model") == {"params": {}}
    assert calls[0][0] == ["uv", "sync", "--project", str(directory), "--locked", "--no-dev"]
    assert len(calls) == 2
    for _, kwargs in calls:
        assert kwargs["cwd"] == directory
        assert_uv_settings(kwargs["env"], directory, shared / group / ".uv-cache", home)
