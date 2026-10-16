"""Offline contracts for dependency-only upgrades; no DB, Git, uv or user imports."""

import os
import subprocess
import tomllib
import traceback
from copy import deepcopy
from pathlib import Path

import pytest
import tomlkit
from packaging.requirements import Requirement
from packaging.version import Version

from config import SoloSettings
from core.apps.projects import templates, upgrade_environment as upgrade
from core.apps.projects.schemas import TemplateVersion

SHA = "d1600fea987b3baaf1f3ad4cfde30b9a5fc90400"
REPOSITORY = "https://gitee.com/genesis-quant/solo-algo-scheme"
ORIGINAL = b'''# Preserve this project's identity and custom build metadata.
[project]
name = "model-ab12"
version = "1.5.7"
description = "My own research, not the template description"
requires-python = ">=3.12,<3.13"
readme = "README.md"
authors = [{name = "Researcher"}]
keywords = ["custom"]
dependencies = [
    "scheme==1.5.7",
    "pandas>=2.2,<2.3",
    "User_Pkg>=1,<2",
]

[project.scripts]
research = "model_ab12.cli:main"

[project.entry-points.research]
custom = "model_ab12:Algo"

[project.urls]
Homepage = "https://research.example"

[project.optional-dependencies]
docs = ["sphinx>=7"]

[build-system]
requires = ["uv_build>=0.9,<0.10"]
build-backend = "uv_build"

[dependency-groups]
dev = ["ipykernel>=6,<7", "pytest>=8"]
lint = ["ruff==0.9.0"]
test = [{include-group = "dev"}, "coverage>=7"]

[tool.uv]
constraint-dependencies = ["scheme==1.5.7", "numpy<3"]
override-dependencies = ["scheme==1.5.7", "requests==2.32.3"]
default-groups = ["dev", "lint"]

[tool.uv.sources]
scheme = {path = "../old-scheme", editable = true}
user-pkg = {path = "../upstream", editable = true}

[[tool.uv.index]]
name = "my-index"
url = "https://index.example/simple"
default = true

[tool.uv.build-backend]
module-name = "model_ab12"
module-root = "src"

[tool.custom]
answer = 42
settings = {unrelated = true}
'''


@pytest.fixture(autouse=True)
def offline(tmp_path, monkeypatch):
    monkeypatch.setattr(SoloSettings, "SHARED_DIR", tmp_path / "shared")
    monkeypatch.setattr(SoloSettings, "HOME_DIR", tmp_path / "home")
    monkeypatch.setattr(SoloSettings, "SCHEME_REPOSITORY", "genesis-quant/solo-algo-scheme")

    def forbidden(*args, **kwargs):
        pytest.fail("Tests must not start processes, access remote metadata or copy template source")

    monkeypatch.setattr(upgrade.subprocess, "run", forbidden)
    for name in ("project_metadata", "gitee_get", "git_output", "create_directory", "unpack_template"):
        monkeypatch.setattr(templates, name, forbidden)


@pytest.fixture
def scheme():
    # The tag is deliberately unrelated: only actual metadata.version is trusted.
    return TemplateVersion(tag="v99.0.0", commit=SHA, version="1.6.0")


@pytest.fixture
def template():
    return tomllib.loads('''
[project]
name = "model"
version = "1.6.4"
description = "Do not copy this description"
requires-python = ">=3.12,<3.13"
dependencies = ["scheme>=1.6.0,<1.7.0", "pydantic>=2,<3", "pandas>=2.2,<3"]

[project.scripts]
wrong = "model:main"

[build-system]
requires = ["different-builder"]
build-backend = "wrong"

[dependency-groups]
dev = ["ipykernel>=6,<8", "pytest>=8", "httpx>=0.28"]
qa = [{include-group = "dev"}, "coverage>=7"]

[tool.uv.sources]
scheme = {git = "https://wrong.example/scheme", branch = "main"}
model = {git = "https://wrong.example/template"}

[tool.template-custom]
ignored = true
''')


def test_merge_preserves_identity_custom_metadata_and_editable_build(scheme, template):
    before, target_before = tomllib.loads(ORIGINAL.decode()), deepcopy(template)
    output = upgrade.updated_project_config(ORIGINAL, template, scheme)
    after = tomllib.loads(output.decode())
    assert isinstance(output, bytes)
    assert output.startswith(b"# Preserve this project's identity")
    assert template == target_before
    assert after["project"]["version"] == "1.6.4"  # Not Scheme's patch or tag.
    for key, value in before["project"].items():
        if key not in ("version", "dependencies"):
            assert after["project"][key] == value
    assert after["build-system"] == before["build-system"]
    assert after["tool"]["custom"] == before["tool"]["custom"]
    assert after["tool"]["uv"]["build-backend"] == before["tool"]["uv"]["build-backend"]
    assert "template-custom" not in after["tool"]
    assert after["tool"]["uv"]["index"] == before["tool"]["uv"]["index"]
    assert after["tool"]["uv"]["default-groups"] == ["dev", "lint"]
    assert after["tool"]["uv"]["sources"] == {
        "user-pkg": {"path": "../upstream", "editable": True},
        "scheme": {"git": REPOSITORY, "rev": SHA},
    }
    dependencies = after["project"]["dependencies"]
    assert dependencies == ["scheme>=1.6.0,<1.7.0", "pandas>=2.2,<2.3", "User_Pkg>=1,<2",
                            "pydantic>=2,<3", "pandas>=2.2,<3"]
    templates.validate_template(after["project"], Version(scheme.version))
    assert after["dependency-groups"]["dev"] == [
        "ipykernel>=6,<7", "pytest>=8", "ipykernel>=6,<8", "httpx>=0.28",
    ]
    assert after["dependency-groups"]["lint"] == before["dependency-groups"]["lint"]
    assert after["dependency-groups"]["test"] == before["dependency-groups"]["test"]
    assert after["dependency-groups"]["qa"] == template["dependency-groups"]["qa"]
    assert upgrade.updated_project_config(output, template, scheme) == output


@pytest.mark.parametrize("name", ["factor", "model", "optimize", "control", "execution"])
def test_no_template_or_root_self_dependency_is_added(scheme, template, name):
    template["project"]["name"] = name
    template["project"]["dependencies"] += [f"{name}>=1.6", "model-ab12>=1.6"]
    template["dependency-groups"]["dev"] += [f"{name}>=1.6", "model-ab12>=1.6"]
    template["tool"]["uv"]["sources"][name] = {"git": "https://example.invalid/template"}
    data = tomllib.loads(upgrade.updated_project_config(ORIGINAL, template, scheme).decode())
    for values in (data["project"]["dependencies"], data["dependency-groups"]["dev"]):
        assert not {name, "model-ab12"} & {Requirement(value).name for value in values}
    assert name not in data["tool"]["uv"]["sources"]


@pytest.mark.parametrize("old", [
    "ScHeMe==1.5.7", "scheme @ git+https://example.invalid/old.git@deadbeef",
    'scheme[custom]>=1.5; python_version >= "3.12"',
])
def test_old_scheme_ranges_direct_urls_and_extra_constraints_are_replaced(scheme, template, old):
    data = tomllib.loads(ORIGINAL.decode())
    data["project"]["dependencies"] += [old]
    data["project"]["optional-dependencies"]["runtime"] = [old, "rich>=13"]
    data["dependency-groups"]["dev"] += [old]
    uv = data["tool"]["uv"]
    uv["constraint-dependencies"] += [old, "pandas<3"]
    uv["override-dependencies"] += [old]
    uv["dev-dependencies"] = [old, "mypy>=1"]
    uv["sources"]["ScHeMe"] = {"url": "https://example.invalid/scheme.whl"}
    template["tool"]["uv"]["constraint-dependencies"] = [old, "pydantic<3"]
    template["tool"]["uv"]["override-dependencies"] = [old, "urllib3<3"]
    template["tool"]["uv"]["dev-dependencies"] = ["rich>=13"]
    after = tomllib.loads(upgrade.updated_project_config(tomlkit.dumps(data).encode(), template, scheme).decode())
    uv = after["tool"]["uv"]
    assert uv["constraint-dependencies"] == ["numpy<3", "pandas<3", "pydantic<3"]
    assert uv["override-dependencies"] == ["requests==2.32.3", "urllib3<3"]
    assert uv["dev-dependencies"] == ["scheme>=1.6.0,<1.7.0", "mypy>=1", "rich>=13"]
    assert after["project"]["optional-dependencies"]["runtime"] == ["scheme>=1.6.0,<1.7.0", "rich>=13"]
    for values in (after["project"]["dependencies"], after["dependency-groups"]["dev"]):
        assert [value for value in values if Requirement(value).name.lower() == "scheme"] == ["scheme>=1.6.0,<1.7.0"]
    assert set(uv["sources"]) == {"scheme", "user-pkg"}
    assert uv["sources"]["scheme"] == {"git": REPOSITORY, "rev": SHA}


@pytest.mark.parametrize("source", [
    {"url": "https://example.invalid/scheme.whl"},
    {"git": REPOSITORY, "tag": "v1.5.7"},
    [{"git": REPOSITORY, "branch": "old", "marker": "sys_platform == 'linux'"}],
])
def test_scheme_source_is_replaced_wholesale(scheme, template, source):
    data = tomllib.loads(ORIGINAL.decode())
    data["tool"]["uv"]["sources"]["scheme"] = source
    result = tomllib.loads(upgrade.updated_project_config(tomlkit.dumps(data).encode(), template, scheme).decode())
    assert result["tool"]["uv"]["sources"]["scheme"] == {"git": REPOSITORY, "rev": SHA}


@pytest.mark.parametrize("version,requirements", [
    ("1.5.0", ["scheme>=1.6.0,<1.7.0"]),
    ("1.6.0", []),
    ("1.6.0", ["scheme>=1.6.1,<1.7.0"]),
    ("1.6.0", ["scheme>=1.6.0,<2"]),
    ("1.6.0rc1", ["scheme>=1.6.0,<1.7.0"]),
    ("1.6.0", ["scheme @ https://example.invalid/scheme.whl"]),
    ("1.6.0", ["scheme>=1.6.0,<1.7.0", "Scheme>=1.6.0,<1.7.0"]),
])
def test_incompatible_template_is_rejected_before_rewriting(scheme, template, version, requirements):
    template["project"].update(version=version, dependencies=requirements)
    with pytest.raises(ValueError):
        upgrade.updated_project_config(ORIGINAL, template, scheme)


def test_new_dependency_sources_are_copied_without_replacing_user_sources(scheme, template):
    template["tool"]["uv"]["sources"].update({
        "new-pkg": {"git": "https://example.invalid/new", "rev": "a" * 40},
        "User_Pkg": {"git": "https://example.invalid/replacement"},
    })
    result = tomllib.loads(upgrade.updated_project_config(ORIGINAL, template, scheme).decode())
    sources = result["tool"]["uv"]["sources"]
    assert sources["new-pkg"] == template["tool"]["uv"]["sources"]["new-pkg"]
    assert sources["user-pkg"] == {"path": "../upstream", "editable": True}
    assert "User_Pkg" not in sources


def test_conflicting_same_package_requirements_are_left_for_resolver(scheme, template, tmp_path, monkeypatch):
    data = tomllib.loads(ORIGINAL.decode())
    data["project"]["dependencies"].append("pydantic<2")
    output = upgrade.updated_project_config(tomlkit.dumps(data).encode(), template, scheme)
    result = tomllib.loads(output.decode())
    assert "pydantic<2" in result["project"]["dependencies"]
    assert "pydantic>=2,<3" in result["project"]["dependencies"]
    assert result["tool"]["uv"]["constraint-dependencies"] == ["numpy<3"]
    (tmp_path / "pyproject.toml").write_bytes(output)

    def failed(args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "", "No solution found: pydantic<2 conflicts with pydantic>=2")

    monkeypatch.setattr(upgrade.subprocess, "run", failed)
    with pytest.raises(upgrade.EnvironmentError, match="No solution found: pydantic"):
        upgrade.install_environment(tmp_path)


def test_install_runs_only_unlocked_uv_sync_at_the_project_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(SoloSettings, "HOME_DIR", Path("home/../home"))
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        monkeypatch.setenv(key, "must-not-inherit")
    monkeypatch.setenv("UV_PROJECT_ENVIRONMENT", "wrong-venv")
    monkeypatch.setenv("UV_LINK_MODE", "copy")
    monkeypatch.setenv("UV_CACHE_DIR", "wrong-cache")
    monkeypatch.setenv("UV_INDEX_URL", "https://index.example/simple")
    directory = Path("shared/projects/model/../model/my project")
    absolute = Path(os.path.abspath(directory))
    absolute.mkdir(parents=True)
    (absolute / "research.ipynb").write_bytes(b"notebook bytes must not change")
    (absolute / "src").mkdir()
    (absolute / "src/user.py").write_bytes(b"invalid Python: never import or migrate me")
    before = {p.relative_to(absolute): p.read_bytes() for p in absolute.rglob("*") if p.is_file()}
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    assert upgrade.install_environment(directory) is None
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ["uv", "sync", "--project", str(absolute), "--python", "3.12", "--managed-python"]
    assert kwargs["cwd"] == absolute
    assert kwargs["timeout"] == 600
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    env = kwargs["env"]
    assert env["UV_PROJECT_ENVIRONMENT"] == str(absolute / ".venv")
    assert env["UV_PYTHON_INSTALL_DIR"] == str(tmp_path / "home/.python")
    assert env["UV_CACHE_DIR"] == str(tmp_path / "shared/projects/.uv-cache")
    assert env["UV_LINK_MODE"] == "hardlink"
    assert env["UV_INDEX_URL"] == "https://index.example/simple"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert all(key not in env for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"))
    assert before == {p.relative_to(absolute): p.read_bytes() for p in absolute.rglob("*") if p.is_file()}
    assert not (tmp_path / "home/.jupyter").exists()


@pytest.mark.parametrize("outcome", ["failed", "timeout", "oserror", "called-process-error"])
def test_all_install_failures_redact_environment_secrets_including_bytes(tmp_path, monkeypatch, outcome):
    secrets = {"CUSTOM_TOKEN": "opaque-token-A", "db_PASSWORD": "opaque-password-B",
               "PLUGIN_SECRET_TEXT": "opaque-secret-C", "MY_API_KEY": "opaque-key-D"}
    for key, value in secrets.items():
        monkeypatch.setenv(key, value)
    payload = " ".join(secrets.values()) + "\n" + secrets["CUSTOM_TOKEN"] + "x" * 2995

    def run(args, **kwargs):
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 600, output=payload.encode(), stderr=payload.encode())
        if outcome == "oserror":
            raise OSError(payload)
        if outcome == "called-process-error":
            raise subprocess.CalledProcessError(1, args, output=payload, stderr=payload)
        return subprocess.CompletedProcess(args, 1, payload, payload)

    monkeypatch.setattr(upgrade.subprocess, "run", run)
    with pytest.raises(upgrade.EnvironmentError) as caught:
        upgrade.install_environment(tmp_path)
    message = str(caught.value)
    assert "[REDACTED]" in message
    formatted = "".join(traceback.format_exception(caught.value))
    assert all(value not in message and value not in formatted for value in secrets.values())
    if outcome == "timeout":
        assert "600" in message and "超时" in message
    else:
        assert "失败" in message
    assert caught.value.__suppress_context__
