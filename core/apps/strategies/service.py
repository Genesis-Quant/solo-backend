import hashlib
import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from uuid import UUID

import tomlkit
from fastapi import HTTPException
from packaging.utils import canonicalize_name
from packaging.version import Version

from config import SoloSettings
from core.apps.projects.models import Project, ProjectVersion
from core.apps.projects.versions import version_directory

KINDS = ("model", "optimize", "control", "execution")
DEFAULTS = {"optimize": "风险平价", "control": "不拒单", "execution": "不拆单"}


def scheme_source(components: dict) -> tuple[str, dict]:
    """使用所选 Model 成果冻结的 Scheme 来源。"""
    selected = components["model"]
    environment = Path(selected["input"]["environment"]["lockfile"]).parent
    config = tomllib.loads((environment / "pyproject.toml").read_text())
    source = config["tool"]["uv"]["sources"]["scheme"].copy()
    if "path" in source:
        source["path"] = str((environment / source["path"]).resolve())
    return selected["scheme_version"], source


def command(args: list[str], cwd: Path, data: dict | None = None) -> str:
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(cwd / ".venv"),
           "UV_PYTHON_INSTALL_DIR": str(SoloSettings.HOME_DIR / ".python"),
           "UV_CACHE_DIR": "/tmp/solo-uv-cache", "UV_LINK_MODE": "copy"}
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    try:
        result = subprocess.run(args, cwd=cwd, env=env, input=json.dumps(data) if data is not None else None,
                                text=True, capture_output=True, timeout=600)
    except subprocess.TimeoutExpired as error:
        raise ValueError("策略环境准备超时，请检查依赖来源是否可访问") from error
    if result.returncode:
        raise ValueError((result.stderr or result.stdout)[-4000:])
    return result.stdout


def selected(session, selections: dict[str, UUID | None]) -> dict:
    result = {}
    for kind in KINDS:
        identifier = selections.get(kind)
        if identifier is None:
            if kind == "model":
                raise HTTPException(422, "请选择 Model 研究版本")
            result[kind] = {"label": DEFAULTS[kind], "version_id": None}
            continue
        version = session.get(ProjectVersion, identifier)
        project = session.get(Project, version.project_id) if version else None
        if not project or project.kind != kind or project.archived or version.status != "success":
            raise HTTPException(422, f"{kind} 必须选择对应类型的成功研究版本")
        source = json.loads((version_directory(version) / "input.json").read_text())
        component = source["algos"][kind]
        result[kind] = {"label": f"{project.name} · v{version.number}", "project_id": str(project.id),
                        "version_id": str(version.id), "scheme_version": version.scheme_version,
                        "component": component, "input": source}
    major = Version(result["model"]["scheme_version"]).major
    if major != 1:
        raise HTTPException(422, "当前策略组装支持 Scheme 1.x")
    if any(item["version_id"] and Version(item["scheme_version"]).major != major for item in result.values()):
        raise HTTPException(422, "各环节必须使用同一 Scheme 大版本")
    return result


def inspect_component(components: dict, kind: str, values: dict | None = None, common: dict | None = None) -> dict:
    item = components[kind]
    source = item if item["version_id"] else components["model"]
    environment = Path(source["input"]["environment"]["lockfile"]).parent
    command(["uv", "sync", "--project", str(environment), "--locked", "--no-dev"], environment)
    data = {"kind": kind, "entry": item.get("component", {}).get("entry"),
            "saved": source["input"]["backtest"] if item["version_id"] else {}}
    if values is not None:
        data.update(values=values, common=common or {}, entries={key: value.get("component", {}).get("entry") for key, value in components.items()})
    output = command([str(environment / ".venv/bin/python"), str(Path(__file__).with_name("parameters.py"))], environment, data)
    return json.loads(output.splitlines()[-1])


def build(components: dict, forms: dict, destination: Path) -> str:
    common = forms["model"]
    inspected = {kind: inspect_component(components, kind, forms[kind], common) for kind in KINDS}
    backtest = inspected["model"]["backtest"]
    # 当前模型的研究选项不决定已选定的链路。
    for key in KINDS:
        backtest.pop(key, None)
    parameters = {}
    for kind in KINDS:
        for key, value in inspected[kind]["params"].items():
            if key in parameters and parameters[key] != value:
                raise ValueError(f"各 Algo 的同名参数 {key} 值不同，无法组成统一策略参数")
            parameters[key] = value
    backtest.update(parameters)
    scheme_version, scheme_location = scheme_source(components)
    candidates, roots, algos = {}, [], {}
    destination.mkdir(parents=True, exist_ok=False)
    for kind, item in components.items():
        if not item["version_id"]:
            continue
        component = item["component"].copy()
        wheel = Path(component["wheel"])
        if hashlib.sha256(wheel.read_bytes()).hexdigest() != component["sha256"]:
            raise ValueError(f"{kind} wheel 哈希不一致")
        environment = Path(item["input"]["environment"]["lockfile"]).parent
        config = tomllib.loads((environment / "pyproject.toml").read_text())
        for name, location in config["tool"]["uv"]["sources"].items():
            name = canonicalize_name(name)
            if name != "scheme":
                location = location.copy()
                if "path" in location:
                    location["path"] = str((environment / location["path"]).resolve())
                locations = candidates.setdefault(name, [])
                if location not in locations:
                    locations.append(location)
        roots.append(f"{component['package']}=={component['version']}")
        algos[kind] = component
    # 所选 Algo 和 Scheme 固定为用户指定的成果，其他依赖交给 uv 解析。
    for component in algos.values():
        candidates[canonicalize_name(component["package"])] = [{"path": component["wheel"]}]
    candidates["scheme"] = [scheme_location]
    roots.append(f"scheme=={scheme_version}")
    # 所有本地 wheel 都复制到本次策略目录，运行不再依赖项目工作区。
    sources, indexes = {}, []
    for name, locations in candidates.items():
        if any("directory" in location or "editable" in location for location in locations):
            raise ValueError("版本环境含有未冻结的目录依赖")
        if not all("path" in location for location in locations):
            if len(locations) != 1:
                raise ValueError(f"依赖 {name} 的来源冲突，无法合并为 wheel 候选集")
            sources[name] = locations[0]
            continue
        directory = destination / "wheels" / name
        directory.mkdir(parents=True, exist_ok=True)
        for location in locations:
            original = Path(location["path"])
            if original.suffix != ".whl":
                raise ValueError("版本环境的本地依赖必须是 wheel")
            target = directory / original.name
            if target.exists() and target.read_bytes() != original.read_bytes():
                raise ValueError(f"依赖 {name} 的同名 wheel 内容不一致")
            shutil.copy2(original, target)
        if len(locations) == 1:
            sources[name] = {"path": str(target)}
        else:
            index = f"wheels-{name}"
            indexes.append({"name": index, "url": str(directory), "format": "flat"})
            sources[name] = {"index": index}
    for component in algos.values():
        component["wheel"] = sources[canonicalize_name(component["package"])]["path"]
    environment = destination / "environment"
    environment.mkdir()
    (environment / "pyproject.toml").write_text(tomlkit.dumps({
        "project": {"name": "strategy-environment", "version": "0.0.0", "requires-python": ">=3.12,<3.13", "dependencies": roots},
        # uv 仅对项目依赖及约束中的包应用 sources；间接依赖也需保留来源，但不继承旧锁版本。
        "tool": {"uv": {"package": False, "sources": sources, "index": indexes,
                        "constraint-dependencies": list(sources)}},
    }))
    command(["uv", "lock", "--project", str(environment)], environment)
    data = {"kind": "strategy", "environment": {"lockfile": str(environment / "uv.lock")},
            "algos": algos, "backtest": backtest, "output": str(destination / "report")}
    (destination / "input.json").write_text(json.dumps(data, ensure_ascii=False, indent=2))
    return scheme_version
