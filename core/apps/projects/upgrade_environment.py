"""Merge the selected environment contract and install it in the project directory."""

from copy import deepcopy
from pathlib import Path

import tomlkit
from config import SoloSettings
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from . import templates
from .environment import sync_environment
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
    sync_environment(directory)
