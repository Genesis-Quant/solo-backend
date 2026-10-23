import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import UUID

import tomlkit
import tomllib
from fastapi import HTTPException
from packaging.utils import canonicalize_name

from core.apps import version_policy as policy
from core.apps.artifacts.service import artifact_component
from core.apps.projects.models import Project, ProjectVersion
from core.apps.projects.versions import version_directory
from core.utils.uv import uv_environment

KINDS = ("model", "optimize", "control", "execution")
DEFAULTS = {"optimize": "风险平价", "control": "不拒单", "execution": "不拆单"}
DEFAULT_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"


def registry_policy(configs: dict, candidates: dict) -> dict:
    """Keep Model registry policy; other results contribute only named source indexes."""
    fields = ("name", "url", "default", "explicit", "format")
    indexes = [
        {key: index[key] for key in fields if key in index}
        for index in configs["model"].get("index", [])
    ]
    named = {}
    for index in indexes:
        name = index.get("name")
        if name is not None:
            if name in named:
                raise ValueError(f"Model 含有歧义的同名 registry 索引 {name}")
            named[name] = index
    for kind, config in configs.items():
        for package, locations in config["sources"].items():
            package = canonicalize_name(package)
            for location in locations if isinstance(locations, list) else [locations]:
                if (
                    package == "scheme" and kind != "model"
                ) or location not in candidates.get(package, []):
                    continue
                name = location.get("index")
                if name is None:
                    continue
                matches = [
                    index
                    for index in config.get("index", [])
                    if index.get("name") == name
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"依赖 {package} 的命名 registry {name} 必须对应唯一索引"
                    )
                index = {key: matches[0][key] for key in fields if key in matches[0]}
                if name in named:
                    if named[name] != index:
                        raise ValueError(
                            f"依赖 {package} 的同名 registry 索引 {name} 配置冲突"
                        )
                else:
                    named[name] = index
                    indexes.append({**index, "default": False, "explicit": True})
    result = {"index": indexes}
    if not any(index.get("default") for index in indexes):
        for key in ("index-url", "extra-index-url"):
            if key in configs["model"]:
                result[key] = configs["model"][key]
        if "index-url" not in result:
            indexes.append({"url": DEFAULT_INDEX_URL, "default": True})
    if "index-strategy" in configs["model"]:
        result["index-strategy"] = configs["model"]["index-strategy"]
    return result


def scheme_source(components: dict) -> tuple[str, list[dict]]:
    selected = components["model"]
    environment = Path(selected["input"]["environment"]["lockfile"]).parent
    config = tomllib.loads((environment / "pyproject.toml").read_text(encoding="utf-8"))
    source = config["tool"]["uv"]["sources"]["scheme"]
    result = []
    for location in source if isinstance(source, list) else [source]:
        location = location.copy()
        if "path" in location:
            location["path"] = str((environment / location["path"]).resolve())
        result.append(location)
    return selected["scheme_version"], result


def command(args: list[str], cwd: Path, data: dict | None = None) -> str:
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(cwd / ".venv"),
        **uv_environment(),
    }
    for key in ("VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=json.dumps(data) if data is not None else None,
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise ValueError("策略环境准备超时，请检查依赖来源是否可访问") from error
    if result.returncode:
        raise ValueError((result.stderr or result.stdout)[-4000:])
    return result.stdout


def has_component(item: dict) -> bool:
    return bool(
        item.get("component") or item.get("artifact_id") or item.get("version_id")
    )


def selected(session, selections: dict[str, UUID | None | dict[str, UUID]]) -> dict:
    result = {}
    artifacts = selections.get("artifacts", {})
    for kind in KINDS:
        if kind in artifacts:
            item = artifact_component(session, artifacts[kind])
            if item["kind"] != kind:
                raise ValueError(f"{kind} 必须选择对应类型的已发布成果")
            result[kind] = item
            continue
        identifier = selections.get(kind)
        if identifier is None:
            result[kind] = {
                "label": DEFAULTS[kind],
                "version_id": None,
                "artifact_id": None,
            }
            continue
        version = session.get(ProjectVersion, identifier)
        project = session.get(Project, version.project_id) if version else None
        if (
            not project
            or project.kind != kind
            or project.archived
            or version.status != "success"
        ):
            raise HTTPException(422, f"{kind} 必须选择对应类型的成功研究版本")
        source = json.loads(
            (version_directory(version) / "input.json").read_text(encoding="utf-8")
        )
        source["environment"]["lockfile"] = str(
            policy.file_in_run(
                version_directory(version), source["environment"]["lockfile"]
            )
        )
        component = source["algos"][kind]
        component["wheel"] = str(
            policy.file_in_run(version_directory(version), component["wheel"])
        )
        snapshots = version.accepted_artifacts or {}
        root_snapshot = snapshots.get(canonicalize_name(component["package"]), {})
        result[kind] = {
            "label": f"{project.name} · v{version.number}",
            "project_id": str(project.id),
            "version_id": str(version.id),
            "artifact_id": None,
            "scheme_version": version.scheme_version,
            "root_sources": root_snapshot.get("sources")
            or policy.project_sources(project),
            "accepted_artifacts": snapshots,
            "component": component,
            "input": source,
        }
    return result


def require_components(components: dict) -> None:
    series = None
    for item in components.values():
        if not has_component(item):
            continue
        reason = policy.source_reason(item["root_sources"])
        if reason:
            raise policy.VersionPolicyError(reason)
        environment = Path(item["input"]["environment"]["lockfile"])
        actual = policy.require_locked_scheme(
            tomllib.loads(environment.read_text(encoding="utf-8"))
        )
        if actual != policy.formal_version(item["scheme_version"]):
            raise ValueError("成果 ActualScheme 与其冻结锁文件不一致")
        policy.require_result_series(item["component"]["version"], actual)
        current = (actual.major, actual.minor)
        if series is not None and current != series:
            raise ValueError("各环节必须使用同一 Scheme 主、次版本")
        series = current
    if series and series[0] != 1:
        raise ValueError("当前策略组装支持 Scheme 1.x")


class ComponentInspector:
    def __init__(self, components: dict):
        require_components(components)
        self.components = components
        self.synced = set()

    def inspect(
        self, kind: str, values: dict | None = None, common: dict | None = None
    ) -> dict:
        item = self.components[kind]
        source = item if has_component(item) else self.components["model"]
        environment = Path(source["input"]["environment"]["lockfile"]).parent
        if environment not in self.synced:
            command(
                ["uv", "sync", "--project", str(environment), "--locked", "--no-dev"],
                environment,
            )
            self.synced.add(environment)
        data = {
            "kind": kind,
            "entry": item.get("component", {}).get("entry"),
            "saved": source["input"].get("backtest", {}) if has_component(item) else {},
        }
        if values is not None:
            data.update(
                values=values,
                common=common or {},
                entries={
                    key: value.get("component", {}).get("entry")
                    for key, value in self.components.items()
                },
            )
        python = (
            environment
            / ".venv"
            / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        output = command(
            [str(python), str(Path(__file__).with_name("parameters.py"))],
            environment,
            data,
        )
        return json.loads(output.splitlines()[-1])


def inspect_component(
    components: dict, kind: str, values: dict | None = None, common: dict | None = None
) -> dict:
    return ComponentInspector(components).inspect(kind, values, common)


def build(components: dict, forms: dict, destination: Path) -> str:
    inspector = ComponentInspector(components)
    inspected = {"model": inspector.inspect("model", forms["model"])}
    common = inspected["model"].get("common", {})
    for kind in KINDS[1:]:
        inspected[kind] = inspector.inspect(kind, forms[kind], common)
    backtest = inspected["model"]["backtest"]
    for key in KINDS:
        backtest.pop(key, None)
    parameters = {}
    for kind in KINDS:
        for key, value in inspected[kind]["params"].items():
            if key in parameters and parameters[key] != value:
                raise ValueError(
                    f"各 Algo 的同名参数 {key} 值不同，无法组成统一策略参数"
                )
            parameters[key] = value
    backtest.update(parameters)
    scheme_version, scheme_locations = scheme_source(components)
    candidates, identities, roots, algos, configs = {}, {}, [], {}, {}
    destination.mkdir(parents=True, exist_ok=False)
    pinned: dict[str, set[str]] = {}
    for kind, item in components.items():
        if not has_component(item):
            continue
        component = {
            key: item["component"][key]
            for key in ("package", "version", "wheel", "entry")
        }
        environment = Path(item["input"]["environment"]["lockfile"]).parent
        config = tomllib.loads(
            (environment / "pyproject.toml").read_text(encoding="utf-8")
        )
        configs[kind] = config["tool"]["uv"]
        frozen = tomllib.loads((environment / "uv.lock").read_text(encoding="utf-8"))
        for p in frozen["package"]:
            if not {"virtual", "directory", "editable"} & p["source"].keys() and (
                p["name"] != "scheme" or kind == "model"
            ):
                pinned.setdefault(p["name"], set()).add(p["version"])
        for name, entries in configs[kind]["sources"].items():
            name = canonicalize_name(name)
            if name == "scheme":
                continue
            for location in entries if isinstance(entries, list) else [entries]:
                location = location.copy()
                locations = candidates.setdefault(name, [])
                if "path" in location:
                    location["path"] = str((environment / location["path"]).resolve())
                    identifier = item["accepted_artifacts"][name]["id"]
                    accepted = identities.setdefault(name, set())
                    if identifier in accepted:
                        continue
                    accepted.add(identifier)
                    locations.append(location)
                elif location not in locations:
                    locations.append(location)
        roots.append(f"{component['package']}=={component['version']}")
        algos[kind] = component
    for component in algos.values():
        candidates[canonicalize_name(component["package"])] = [
            {"path": component["wheel"]}
        ]
    candidates["scheme"] = scheme_locations
    roots.append(f"scheme=={scheme_version}")
    registry = registry_policy(configs, candidates)
    sources, indexes = {}, registry["index"]
    for name, locations in candidates.items():
        if any(
            "directory" in location or "editable" in location for location in locations
        ):
            raise ValueError("版本环境含有未冻结的目录依赖")
        if not all("path" in location for location in locations):
            sources[name] = locations[0] if len(locations) == 1 else locations
            continue
        directory = destination / "wheels" / name
        directory.mkdir(parents=True, exist_ok=True)
        copied = set()
        for location in locations:
            original = Path(location["path"])
            if original.suffix != ".whl":
                raise ValueError("版本环境的本地依赖必须是 wheel")
            target = directory / original.name
            if target.name in copied:
                raise ValueError(f"依赖 {name} 的同名 wheel 来源冲突")
            copied.add(target.name)
            shutil.copy2(original, target)
        if len(copied) == 1:
            sources[name] = {
                "path": Path(
                    os.path.relpath(target, destination / "environment")
                ).as_posix()
            }
        else:
            index = f"wheels-{name}"
            if any(item.get("name") == index for item in indexes):
                raise ValueError(f"本地 wheel 索引 {index} 与继承的 registry 名称冲突")
            indexes.append(
                {
                    "name": index,
                    "url": Path(
                        os.path.relpath(directory, destination / "environment")
                    ).as_posix(),
                    "format": "flat",
                }
            )
            sources[name] = {"index": index}
    for component in algos.values():
        wheel = (
            destination
            / "environment"
            / sources[canonicalize_name(component["package"])]["path"]
        )
        component["wheel"] = (
            wheel.resolve().relative_to(destination.resolve()).as_posix()
        )
    # 各组件冻结版本不一致的包不加约束，否则互斥的 == 约束会让 uv lock 无解。
    constraints = sorted(
        f"{name}=={next(iter(versions))}"
        for name, versions in pinned.items()
        if len(versions) == 1
    )
    environment = destination / "environment"
    environment.mkdir()
    (environment / "pyproject.toml").write_text(
        tomlkit.dumps(
            {
                "project": {
                    "name": "strategy-environment",
                    "version": "0.0.0",
                    "requires-python": ">=3.12,<3.13",
                    "dependencies": roots,
                },
                "tool": {
                    "uv": {
                        "package": False,
                        "sources": sources,
                        **registry,
                        "constraint-dependencies": constraints,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    command(["uv", "lock", "--project", str(environment)], environment)
    frozen = policy.require_locked_scheme(
        tomllib.loads((environment / "uv.lock").read_text(encoding="utf-8"))
    )
    if frozen != policy.formal_version(scheme_version):
        raise ValueError("策略冻结环境改变了所选 Model 的 ActualScheme")
    data = {
        "kind": "strategy",
        "environment": {"lockfile": "environment/uv.lock"},
        "algos": algos,
        "backtest": backtest,
        "output": "report",
    }
    (destination / "input.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return scheme_version
