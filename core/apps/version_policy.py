"""Single-source release admission. Historical reads never rewrite or migrate records."""

import hashlib
import json
import re
import tomllib
from email.parser import BytesParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from uuid import UUID
from zipfile import BadZipFile, ZipFile

from packaging.utils import canonicalize_name
from packaging.version import Version
from sqlalchemy import select

from config import BACKEND_ROOT, SoloSettings

POLICY_FILE = BACKEND_ROOT / "version-policy.json"
RESEARCH_PACKAGE = re.compile(r"(factor|model|optimize|control|execution)-([0-9a-f]{4})")


class VersionPolicyError(ValueError):
    def __init__(self, reason: str, code: str = "version_retired"):
        super().__init__(reason)
        self.code = code
        self.reason = reason

    @property
    def detail(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def load_policy() -> dict:
    policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
    if policy["grandfathered_runs"]:
        raise VersionPolicyError("当前没有确认的退役在途任务，不支持启用 grandfather 例外", "invalid_version_policy")
    return policy


def formal_version(value: str) -> Version:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", value):
        raise VersionPolicyError("无法验证正式版本来源；仅允许读取历史", "invalid_version_source")
    return Version(value)


def retired_release(kind: str, tag: str | None = None, commit: str | None = None) -> dict | None:
    return next((release for release in load_policy()["retired_releases"][kind]
                 if (tag is not None and tag == release["tag"])
                 or (commit is not None and commit.lower() == release["commit"].lower())), None)


def release_reason(kind: str, *, tag: str | None = None, commit: str | None = None,
                   version: str | None = None) -> str | None:
    release = retired_release(kind, tag, commit)
    if release:
        return release["reason"]
    # A tag is a release identity, not a project's candidate packageVersion. Actual
    # metadata, when available, takes precedence over an otherwise unknown tag.
    if version is None:
        version = tag.removeprefix("v") if tag and re.fullmatch(r"v?\d+\.\d+\.\d+", tag) else None
    if version is None:
        return None
    actual = formal_version(version)
    minimum = formal_version(load_policy()["minimum_versions"][kind])
    if actual < minimum:
        return f"{kind} {version} 已退役，低于 minimumActiveVersion {minimum}；仅允许读取历史"
    return None


def require_release(kind: str, **source) -> None:
    reason = release_reason(kind, **source)
    if reason:
        raise VersionPolicyError(reason)


def project_sources(project) -> dict:
    return {
        "scheme": {"tag": project.scheme_version, "commit": project.scheme_commit},
        # New projects retain verified creation metadata, never the mutable
        # candidate counter in workspace pyproject.toml. Legacy semver tags keep
        # release_reason's documented fallback; unknown custom tags fail closed.
        "template": {"tag": project.template_tag, "commit": project.template_commit,
                     "version": getattr(project, "template_package_version", None)},
    }


def source_reason(sources: dict) -> str | None:
    for kind in ("scheme", "template"):
        source = sources.get(kind, {})
        if not source or not (source.get("tag") or source.get("commit") or source.get("version")):
            raise VersionPolicyError(f"缺少 {kind} release 来源；仅允许读取历史", "invalid_version_source")
        release = retired_release(kind, source.get("tag"), source.get("commit"))
        if release:
            return release["reason"]
        # Scheme metadata comes from the applicable frozen lock, not tag digits.
        reason = (release_reason(kind, version=source.get("version")) if kind == "scheme"
                  else release_reason(kind, **source))
        if reason:
            return reason
        if kind == "template" and source and not (
            source.get("version") or re.fullmatch(r"v?\d+\.\d+\.\d+", source.get("tag") or "")
        ):
            raise VersionPolicyError("无法验证模板 release 来源；仅允许读取历史", "invalid_version_source")
    return None


def locked_scheme(lock: dict) -> tuple[Version, str | None]:
    packages = [item for item in lock["package"] if item["name"] == "scheme"]
    if len(packages) != 1:
        raise ValueError("锁文件必须含唯一 Scheme")
    package = packages[0]
    version = formal_version(package["version"])
    git = package.get("source", {}).get("git", "")
    commit = git.rsplit("#", 1)[-1] if "#" in git else None
    return version, commit


def require_locked_scheme(lock: dict) -> Version:
    version, commit = locked_scheme(lock)
    require_release("scheme", commit=commit, version=str(version))
    return version


def project_path(project) -> Path:
    return SoloSettings.SHARED_DIR / "projects" / project.kind / project.name


def require_project(project, *, lock: dict | None = None) -> Version:
    reason = source_reason(project_sources(project))
    if reason:
        raise VersionPolicyError(reason)
    try:
        if lock is None:
            lock = tomllib.loads((project_path(project) / "uv.lock").read_text(encoding="utf-8"))
        return require_locked_scheme(lock)
    except VersionPolicyError:
        raise
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise VersionPolicyError("项目环境尚未记录有效的 Scheme 主、次版本", "invalid_version_source") from error


def project_retirement(project) -> str | None:
    try:
        require_project(project)
    except VersionPolicyError as error:
        return error.reason
    return None


def run_path(identifier: UUID) -> Path:
    return SoloSettings.SHARED_DIR / "runs" / str(identifier)


def task_files(directory: Path, kind: str, *, input_sha256: str | None = None,
               lock_sha256: str | None = None) -> tuple[dict, Path, dict]:
    """Read only the mapped run's input and its in-run uv.lock, never a caller path."""
    root = (SoloSettings.SHARED_DIR / "runs").resolve()
    if directory.is_symlink():
        raise ValueError("任务目录不能重定向至其他 Run")
    directory = directory.resolve()
    if directory == root or not directory.is_relative_to(root):
        raise ValueError("任务目录必须位于对应 runs/{id} 中")
    input_file = (directory / "input.json").resolve()
    if not input_file.is_relative_to(directory):
        raise ValueError("任务输入不能指向版本目录外")
    content = input_file.read_bytes()
    if input_sha256 is not None and hashlib.sha256(content).hexdigest() != input_sha256:
        raise ValueError("input_sha256 与已接受的真实任务输入不一致")
    data = json.loads(content)
    if data["kind"] != kind:
        raise ValueError("任务 kind 与数据库记录不一致")
    lockfile = (directory / data["environment"]["lockfile"]).resolve()
    if lockfile.name != "uv.lock" or not lockfile.is_relative_to(directory):
        raise ValueError("任务锁文件必须位于对应 runs/{id} 中")
    content = lockfile.read_bytes()
    if lock_sha256 is not None and hashlib.sha256(content).hexdigest() != lock_sha256:
        raise ValueError("lock_sha256 与已接受的真实任务锁文件不一致")
    lock = tomllib.loads(content.decode("utf-8"))
    if any({"editable", "directory"} & item.get("source", {}).keys() for item in lock["package"]):
        raise ValueError("正式任务不能依赖未冻结的源码目录")
    return data, lockfile, lock


def version_retirement(version) -> str | None:
    """An old result keeps its own ActualScheme, even if its project is now active."""
    try:
        if version.scheme_version:
            return release_reason("scheme", version=version.scheme_version)
        data = json.loads((run_path(version.id) / "input.json").read_text(encoding="utf-8"))
        _, _, lock = task_files(run_path(version.id), data["kind"])
        actual, commit = locked_scheme(lock)
        return release_reason("scheme", commit=commit, version=str(actual))
    except (OSError, ValueError, KeyError, TypeError):
        return "成果尚未记录有效的 ActualScheme；仅允许读取历史"


def require_result_series(package_version: str, scheme: Version) -> None:
    """Candidate versions follow the active minor, but are not release identities."""
    package = formal_version(package_version)
    require_release("scheme", version=str(scheme))
    if (package.major, package.minor) != (scheme.major, scheme.minor):
        raise ValueError("候选成果必须使用实际 Scheme 的主、次版本")


def file_in_run(directory: Path, value: str) -> Path:
    path = (directory / value).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError("任务本地产物必须位于对应 runs/{id} 中")
    return path


def local_lock_path(base: Path, value: str) -> Path:
    """Decode only filesystem/file URLs; never turn a remote index into a local path."""
    if not isinstance(value, str) or not value:
        raise ValueError("锁文件缺少本地 wheel 来源")
    if Path(value).is_absolute():
        return Path(value).resolve()
    location = urlsplit(value)
    if location.query or location.fragment or location.netloc or location.scheme not in {"", "file"}:
        raise ValueError("锁文件 wheel 来源必须是本地文件，不能使用远端 registry")
    path = Path(url2pathname(location.path) if location.scheme == "file" else unquote(location.path))
    return (base / path).resolve()


def locked_local_wheel(directory: Path, lockfile: Path, package: dict) -> Path:
    """Resolve the exact artifact declared by uv, not another wheel in the index."""
    source = package.get("source", {})
    if set(source) == {"path"}:
        return file_in_run(directory, str(local_lock_path(lockfile.parent, source["path"])))
    if set(source) != {"registry"}:
        raise ValueError(f"研究依赖 {package['name']} 缺少冻结本地 wheel 来源")
    registry = local_lock_path(lockfile.parent, source["registry"])
    if not registry.is_relative_to(directory.resolve()) or not registry.is_dir():
        raise ValueError("任务本地 registry 必须位于对应 runs/{id} 中")
    wheels = package.get("wheels", [])
    if len(wheels) != 1:
        raise ValueError("本地 registry 必须锁定唯一准确 wheel，不能扫描或回退其他候选")
    wheel, = wheels
    if ("path" in wheel) == ("url" in wheel):
        raise ValueError("本地 registry wheel 必须声明唯一 path 或 URL")
    path = local_lock_path(registry if "path" in wheel else lockfile.parent, wheel.get("path", wheel.get("url")))
    path = file_in_run(directory, str(path))
    if not path.is_relative_to(registry) or path.suffix != ".whl":
        raise ValueError("锁定 wheel 必须位于其本地 registry 中")
    # uv omits hashes for local wheels.path; when a lock provides a digest it is
    # binding. METADATA/series/entry and database source checks still always run.
    if "hash" in wheel:
        digest = wheel["hash"]
        if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError("锁定 wheel 必须使用有效 sha256")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest.removeprefix("sha256:"):
            raise ValueError("锁定 wheel sha256 不一致")
    return path


def validate_wheel(directory: Path, component: dict, *, package: str, version: str, scheme: Version) -> None:
    require_result_series(version, scheme)
    if canonicalize_name(component["package"]) != package or component["version"] != version:
        raise ValueError("任务 package/version 与 rootProject 数据库成果不一致")
    entry = component["entry"]
    module = package.replace("-", "_")
    if not isinstance(entry, str) or not re.fullmatch(rf"{re.escape(module)}(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", entry):
        raise ValueError("任务 entry 必须属于对应 rootProject 包")
    path = file_in_run(directory, component["wheel"])
    if hashlib.sha256(path.read_bytes()).hexdigest() != component["sha256"]:
        raise ValueError("任务 wheel sha256 不一致")
    try:
        with ZipFile(path) as wheel:
            names = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
            if len(names) != 1:
                raise ValueError("wheel 必须含唯一 METADATA")
            metadata = BytesParser().parsebytes(wheel.read(names[0]))
            if canonicalize_name(metadata["Name"]) != package or metadata["Version"] != version:
                raise ValueError("wheel METADATA 与冻结 package/version 不一致")
            from core.apps.projects.templates import validate_template

            validate_template({"version": metadata["Version"],
                               "dependencies": metadata.get_all("Requires-Dist", [])}, scheme)
            entry_module = entry.split(":", 1)[0].replace(".", "/")
            if not any(name in wheel.namelist() for name in (entry_module + ".py", entry_module + "/__init__.py")):
                raise ValueError("wheel 缺少声明的 entry 模块")
    except BadZipFile as error:
        raise ValueError("任务 wheel 不是有效归档") from error


def package_name(project) -> str:
    name = getattr(project, "package_name", None)
    return canonicalize_name(name if isinstance(name, str) and name else f"{project.kind}-{project.id.hex[:4]}")


def task_components(data: dict) -> dict[str, dict]:
    items = [data["factor"]] if data["kind"] == "factor" else list(data["algos"].values())
    components = {canonicalize_name(item["package"]): item for item in items if item}
    if len(components) != len([item for item in items if item]):
        raise ValueError("任务组件 package 身份重复")
    return components


def local_task_packages(lock: dict) -> dict[str, dict]:
    packages = {}
    for item in lock["package"]:
        source = item.get("source", {})
        local = "path" in source or ("registry" in source and (
            Path(source["registry"]).is_absolute() or urlsplit(source["registry"]).scheme in {"", "file"}
        ))
        if local or RESEARCH_PACKAGE.fullmatch(canonicalize_name(item["name"])):
            name = canonicalize_name(item["name"])
            if name in packages:
                raise ValueError(f"锁文件含有重复本地产物：{name}")
            packages[name] = item
    return packages


def snapshot_values(value) -> dict[str, dict]:
    if value is None:
        return {}
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    result = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("package"), str):
            raise ValueError("冻结产物快照无效")
        name = canonicalize_name(item["package"])
        if name in result and result[name] != item:
            raise ValueError(f"冻结产物身份冲突：{name}")
        result[name] = item
    return result


def capture_task_artifacts(session, record, *, project=None, directory: Path | None = None,
                           kind: str, historical: bool = False) -> dict[str, dict]:
    """在接受任务前绑定准确产物；执行检查永远不重新推导来源身份。"""
    from core.apps.artifacts.models import ResearchArtifact
    from core.apps.artifacts.service import artifact_snapshot, lock_registry, register_asset, register_file, require_artifact
    from core.apps.projects.models import Project, ProjectVersion

    lock_registry(session)
    directory = directory or run_path(record.id)
    data, lockfile, lock = task_files(directory, kind)
    actual, _ = locked_scheme(lock)
    if record.input_sha256 and record.input_sha256 != hashlib.sha256((directory / "input.json").read_bytes()).hexdigest():
        raise ValueError("冻结输入不符合数据库原始接受记录")
    if record.lock_sha256 and record.lock_sha256 != hashlib.sha256(lockfile.read_bytes()).hexdigest():
        raise ValueError("冻结锁文件不符合数据库原始接受记录")
    components = task_components(data)
    hints: dict[str, list[dict]] = {}

    def add_hints(value) -> None:
        for name, snapshot in snapshot_values(value).items():
            candidates = hints.setdefault(name, [])
            if snapshot not in candidates:
                candidates.append(snapshot)

    if project is not None:
        add_hints(getattr(project, "artifact_dependencies", None))
    roots = {}
    if project is not None:
        roots[package_name(project)] = project
    selections = getattr(record, "components", {}) if project is None else {}
    for stage, selection in selections.items():
        if not isinstance(selection, dict):
            raise ValueError("策略所选产物身份无效")
        add_hints(selection.get("accepted_artifacts"))
        add_hints(selection.get("dependencies"))
        artifact_id = selection.get("artifact_id")
        if artifact_id:
            artifact = session.get(ResearchArtifact, UUID(artifact_id))
            if artifact is None or artifact.kind != stage:
                raise ValueError("所选发布产物不存在或类型不符")
            hint = artifact_snapshot(artifact)
            component = components.get(hint["package"])
            if component is None or any(component.get(key) != hint.get(key) for key in ("package", "version", "entry", "sha256")):
                raise ValueError("策略组件与所选独立发布产物不一致")
            add_hints([hint])
            add_hints(hint.get("dependencies"))
        elif selection.get("version_id"):
            version = session.get(ProjectVersion, UUID(selection["version_id"]))
            root = session.get(Project, version.project_id) if version is not None else None
            if root is None or root.kind != stage or str(root.id) != selection.get("project_id"):
                raise ValueError("策略初始选定研究版本不存在或类型不符")
            roots[package_name(root)] = root
            add_hints(getattr(version, "accepted_artifacts", None))
            original = json.loads((run_path(version.id) / "input.json").read_text(encoding="utf-8"))["algos"][stage]
            candidate = components.get(original["package"])
            if candidate is None or any(candidate[key] != original[key] for key in ("package", "version", "entry", "sha256")):
                raise ValueError("策略组件不符合初始选定成果的准确 wheel")
        expected = selection.get("component_snapshot")
        if expected:
            candidate = components.get(expected.get("package"))
            if candidate is None or any(candidate.get(key) != expected.get(key) for key in ("package", "version", "entry", "sha256")):
                raise ValueError("策略组件与所选产物快照不一致")

    legacy_roots = None
    captured = {}
    for name, package in local_task_packages(lock).items():
        wheel = locked_local_wheel(directory, lockfile, package)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        component = components.get(name)
        if component is not None and (
            wheel != file_in_run(directory, component["wheel"]) or component["sha256"] != digest
        ):
            raise ValueError("输入组件与冻结锁文件的准确 wheel 不一致")
        candidates = hints.get(name, [])
        matches = [hint for hint in candidates if hint.get("version") == package["version"] and hint.get("sha256") == digest]
        artifact = None
        if matches:
            artifact = require_artifact(session, matches[0], wheel=wheel)
        elif candidates and project is not None:
            raise ValueError(f"任务产物不符合已安装的准确依赖：{name}")
        if artifact is None:
            found = session.scalar(select(ResearchArtifact).where(ResearchArtifact.sha256 == digest))
            if isinstance(found, ResearchArtifact):
                artifact = require_artifact(session, artifact_snapshot(found), wheel=wheel)
        if artifact is None:
            root = roots.get(name)
            if root is None and RESEARCH_PACKAGE.fullmatch(name):
                if legacy_roots is None:
                    legacy_roots = {}
                    for candidate in session.scalars(select(Project)):
                        legacy_roots.setdefault(package_name(candidate), []).append(candidate)
                matches = legacy_roots.get(name, [])
                if len(matches) != 1:
                    raise ValueError(f"缺少准确研究产物来源：{name}")
                root = matches[0]
            if root is not None:
                if not historical:
                    reason = source_reason(project_sources(root))
                    if reason:
                        raise VersionPolicyError(reason)
                # Only this Project's own run can supply its ActualScheme here.
                # An upstream copy must prove exact bytes against the original
                # owner's frozen run/source inputs, independently of this lock.
                artifact = register_file(session, wheel, root, component=component,
                                         scheme_version=str(actual) if root is project else None,
                                         historical=historical)
            else:
                sources = project_sources(project) if project is not None else selections.get("model", {}).get("root_sources", {})
                artifact = register_asset(session, wheel, sources=sources, scheme_version=str(actual),
                                          component=component, historical=historical)
        snapshot = artifact_snapshot(artifact)
        if snapshot["package"] != name or snapshot["version"] != package["version"] or snapshot["sha256"] != digest:
            raise ValueError("产物注册身份与冻结锁文件不一致")
        captured[name] = snapshot
    if set(components) - set(captured):
        raise ValueError("输入组件缺少对应的冻结产物")
    return captured


def task_source_reasons(session, record, project, lock: dict, directory: Path, data: dict) -> list[str]:
    snapshots = snapshot_values(getattr(record, "accepted_artifacts", None))
    packages = local_task_packages(lock)
    if set(snapshots) != set(packages):
        raise ValueError("任务缺少准确接受的产物快照，必须先完成显式迁移或重新提交")
    components = task_components(data)
    actual, _ = locked_scheme(lock)
    reasons = []
    for name, package in packages.items():
        snapshot = snapshots[name]
        wheel = locked_local_wheel(directory, directory / data["environment"]["lockfile"], package)
        if (snapshot.get("version") != package["version"] or snapshot.get("package") != name
                or not isinstance(snapshot.get("sha256"), str)
                or hashlib.sha256(wheel.read_bytes()).hexdigest() != snapshot["sha256"]):
            raise ValueError(f"任务 wheel 不符合数据库接受的准确产物：{name}")
        if RESEARCH_PACKAGE.fullmatch(name):
            reason = source_reason(snapshot.get("sources", {}))
            if reason:
                reasons.append(reason)
            require_result_series(package["version"], actual)
            component = components.get(name) or {
                "package": name, "version": package["version"], "wheel": str(wheel),
                "sha256": snapshot["sha256"], "entry": snapshot.get("entry"),
            }
            if (component.get("entry") != snapshot.get("entry") or component.get("sha256") != snapshot["sha256"]
                    or file_in_run(directory, component["wheel"]) != wheel):
                raise ValueError("输入组件不符合准确接受的产物快照")
            validate_wheel(directory, component, package=name, version=package["version"], scheme=actual)
        elif name in components:
            raise ValueError("任务组件必须是研究产物")
        else:
            try:
                with ZipFile(wheel) as archive:
                    files = [value for value in archive.namelist() if value.endswith(".dist-info/METADATA")]
                    if len(files) != 1:
                        raise ValueError("依赖 wheel 缺少唯一 METADATA")
                    metadata = BytesParser().parsebytes(archive.read(files[0]))
                    if canonicalize_name(metadata["Name"]) != name or metadata["Version"] != package["version"]:
                        raise ValueError("依赖 wheel METADATA 与接受的产物身份不一致")
            except BadZipFile as error:
                raise ValueError("依赖 wheel 不是有效归档") from error
    return reasons


def accepted_runtime_artifacts(record, directory: Path, kind: str) -> list[dict[str, str]]:
    _, lockfile, lock = task_files(directory, kind)
    snapshots = snapshot_values(getattr(record, "accepted_artifacts", None))
    packages = local_task_packages(lock)
    if set(packages) != set(snapshots):
        raise ValueError("任务缺少准确接受的产物快照")
    return [{"package": name, "version": snapshots[name]["version"], "sha256": snapshots[name]["sha256"],
             "wheel": locked_local_wheel(directory, lockfile, package).relative_to(directory.resolve()).as_posix()}
            for name, package in sorted(packages.items())]


def check_task(session, record, *, project=None, directory: Path | None = None,
               kind: str, input_sha256: str | None = None, lock_sha256: str | None = None,
               require_accepted: bool = False) -> bool:
    directory = directory or run_path(record.id)
    data, lockfile, lock = task_files(directory, kind,
                                    input_sha256=input_sha256, lock_sha256=lock_sha256)
    actual, commit = locked_scheme(lock)
    reason = release_reason("scheme", commit=commit, version=str(actual))
    if reason:
        raise VersionPolicyError(reason)
    if ((record.input_sha256 and record.input_sha256 != hashlib.sha256((directory / "input.json").read_bytes()).hexdigest())
            or (record.lock_sha256 and record.lock_sha256 != hashlib.sha256(lockfile.read_bytes()).hexdigest())):
        raise ValueError("冻结文件已改变，不符合数据库准确接受记录")
    if require_accepted and (
        record.status not in {"queued", "running"} or not record.input_sha256 or not record.lock_sha256
        or record.input_sha256 != input_sha256 or record.lock_sha256 != lock_sha256
    ):
        raise ValueError("任务尚未接受，或 input/lock 哈希不符合数据库接受记录")
    if record.scheme_version and formal_version(record.scheme_version) != actual:
        raise ValueError("记录的 ActualScheme 与冻结锁文件不一致")
    if project is not None:
        component = data["factor"] if kind == "factor" else data["algos"][kind]
        validate_wheel(directory, component, package=package_name(project), version=record.package_version, scheme=actual)
    elif hasattr(record, "components"):
        expected = {name for name, item in record.components.items()
                    if item.get("version_id") or item.get("artifact_id") or item.get("component_snapshot")}
        if set(data["algos"]) != expected:
            raise ValueError("策略 algos 必须准确匹配已接受的组件")
        accepted = snapshot_values(getattr(record, "accepted_artifacts", None))
        for stage in expected:
            component = data["algos"][stage]
            snapshot = accepted.get(canonicalize_name(component["package"]))
            if snapshot is None or snapshot.get("kind") != stage:
                raise ValueError("策略组件缺少准确接受的类型与产物快照")
            selected_id = record.components[stage].get("artifact_id")
            if selected_id and selected_id != (snapshot.get("artifact_id") or snapshot.get("id")):
                raise ValueError("策略组件与所选独立产物 ID 不一致")
            expected_component = record.components[stage].get("component_snapshot")
            if expected_component and any(component.get(key) != expected_component.get(key)
                                          for key in ("package", "version", "entry", "sha256")):
                raise ValueError("策略组件与所选产物快照不一致")
            validate_wheel(directory, component, package=snapshot["package"], version=snapshot["version"], scheme=actual)
    reasons = task_source_reasons(session, record, project, lock, directory, data)
    reason = release_reason("scheme", commit=commit, version=str(actual))
    if reason:
        reasons.insert(0, reason)
    if reasons:
        raise VersionPolicyError(reasons[0])
    if project is not None:
        require_result_series(record.package_version, actual)
    else:
        require_result_series(str(actual), actual)
    return False
