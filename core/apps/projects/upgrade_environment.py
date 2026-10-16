"""Merge the selected environment contract and install it in the project directory."""

import os
import subprocess
from copy import deepcopy
from pathlib import Path

import tomlkit
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from config import SoloSettings
from core.utils.uv import uv_environment

from . import templates
from .environment import EnvironmentError
from .schemas import TemplateVersion


def updated_project_config(original: bytes, template: dict, scheme: TemplateVersion) -> bytes:
    """Keep user metadata and dependency intersections; let uv resolve conflicts."""
    selected = Version(scheme.version)
    target = template["project"]
    templates.validate_template(target, selected)
    document = tomlkit.parse(original.decode("utf-8"))
    project = document["project"]
    omit = {canonicalize_name(project["name"]), canonicalize_name(target["name"])}
    requirement = f"scheme>={selected.major}.{selected.minor}.0,<{selected.major}.{selected.minor + 1}.0"

    def merge(existing, additions, scheme_requirement, *, omit=()):
        result = []
        seen = set()
        for values, added in ((existing, False), (additions, True)):
            for value in values:
                if isinstance(value, str):
                    dependency = Requirement(value)
                    name = canonicalize_name(dependency.name)
                    if name == "scheme":
                        if scheme_requirement is None:
                            continue
                        value = scheme_requirement
                        dependency = Requirement(value)
                    elif added and name in omit:
                        continue
                    if (added or name == "scheme") and dependency in seen:
                        continue
                    seen.add(dependency)
                elif added and value in result:
                    continue
                result.append(deepcopy(value))
        return result

    project["version"] = target["version"]
    project["dependencies"] = merge(
        project.get("dependencies", []), target.get("dependencies", []), requirement, omit=omit,
    )
    for group, values in project.get("optional-dependencies", {}).items():
        project["optional-dependencies"][group] = merge(values, [], requirement)

    target_groups = template.get("dependency-groups", {})
    if "dependency-groups" in document or target_groups:
        groups = document.setdefault("dependency-groups", {})
        for group in dict.fromkeys([*groups, *target_groups]):
            groups[group] = merge(groups.get(group, []), target_groups.get(group, []), requirement, omit=omit)

    uv = document.setdefault("tool", {}).setdefault("uv", {})
    target_uv = template.get("tool", {}).get("uv", {})
    for key in ("constraint-dependencies", "override-dependencies", "dev-dependencies"):
        if key in uv or key in target_uv:
            uv[key] = merge(
                uv.get(key, []), target_uv.get(key, []),
                requirement if key == "dev-dependencies" else None, omit=omit,
            )
    sources = uv.setdefault("sources", {})
    for name in list(sources):
        if canonicalize_name(name) == "scheme":
            del sources[name]
    sources["scheme"] = {"git": f"https://gitee.com/{SoloSettings.SCHEME_REPOSITORY}", "rev": scheme.commit}
    source_names = {canonicalize_name(name) for name in sources}
    for name, source in target_uv.get("sources", {}).items():
        if canonicalize_name(name) not in source_names | omit:
            sources[name] = deepcopy(source)
            source_names.add(canonicalize_name(name))
    return tomlkit.dumps(document).encode("utf-8")


def install_environment(directory: Path) -> None:
    directory = Path(os.path.abspath(directory))
    env = {
        **os.environ,
        **uv_environment(directory),
        "UV_PROJECT_ENVIRONMENT": str(directory / ".venv"),
        "UV_PYTHON_INSTALL_DIR": str(Path(os.path.abspath(SoloSettings.HOME_DIR)) / ".python"),
        "UV_PYTHON_BIN_DIR": "/tmp/solo-python-bin",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    secrets = {
        value for key, value in env.items()
        if value and any(word in key.upper() for word in ("TOKEN", "PASSWORD", "SECRET", "API_KEY"))
    }
    secrets.update(value for value in (SoloSettings.GITEE_TOKEN, SoloSettings.JUPYTER_TOKEN) if value)

    def redacted(*values):
        message = "\n".join(value.decode("utf-8", errors="replace") if isinstance(value, bytes)
                            else str(value or "") for value in values)
        for value in sorted(secrets, key=len, reverse=True):
            message = message.replace(value, "[REDACTED]")
        return message

    try:
        result = subprocess.run(
            ["uv", "sync", "--project", str(directory), "--python", "3.12", "--managed-python"],
            cwd=directory, env=env, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as error:
        raise EnvironmentError(f"项目环境安装超时（600 秒）：\n{redacted(error.stdout, error.stderr)}") from None
    except (OSError, subprocess.SubprocessError) as error:
        output = redacted(str(error), getattr(error, "stdout", None), getattr(error, "stderr", None))
        raise EnvironmentError(f"项目环境安装失败：\n{output}") from None
    if result.returncode:
        raise EnvironmentError(f"项目环境安装失败：\n{redacted(result.stdout, result.stderr)}") from None
