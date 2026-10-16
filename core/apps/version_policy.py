"""Release admission and frozen task metadata; no file content identities."""

import json
import re
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname
from uuid import UUID

from packaging.utils import canonicalize_name
from packaging.version import Version

from config import BACKEND_ROOT, SoloSettings

POLICY_FILE = BACKEND_ROOT / "version-policy.json"
RESEARCH_PACKAGE = re.compile(r"(factor|model|optimize|control|execution)-([0-9a-f]{4})")
_policy_key: tuple[Path, int] | None = None
_policy_data: dict | None = None


class VersionPolicyError(ValueError):
    def __init__(self, reason: str, code: str = "version_retired"):
        super().__init__(reason)
        self.code, self.reason = code, reason

    @property
    def detail(self) -> dict:
        return {"code": self.code, "reason": self.reason}


def load_policy() -> dict:
    global _policy_key, _policy_data
    key = (POLICY_FILE, POLICY_FILE.stat().st_mtime_ns)
    if key != _policy_key:
        policy = json.loads(POLICY_FILE.read_text(encoding="utf-8"))
        if policy["grandfathered_runs"]:
            raise VersionPolicyError("当前没有确认的退役在途任务，不支持启用 grandfather 例外", "invalid_version_policy")
        _policy_key, _policy_data = key, policy
    return _policy_data


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
        "template": {"tag": project.template_tag, "commit": project.template_commit,
                     "version": project.template_package_version},
    }


def source_reason(sources: dict) -> str | None:
    for kind in ("scheme", "template"):
        source = sources.get(kind, {})
        if not source or not (source.get("tag") or source.get("commit") or source.get("version")):
            raise VersionPolicyError(f"缺少 {kind} release 来源；仅允许读取历史", "invalid_version_source")
        release = retired_release(kind, source.get("tag"), source.get("commit"))
        if release:
            return release["reason"]
        reason = (release_reason(kind, version=source.get("version")) if kind == "scheme"
                  else release_reason(kind, **source))
        if reason:
            return reason
        if kind == "template" and not (source.get("version") or re.fullmatch(r"v?\d+\.\d+\.\d+", source.get("tag") or "")):
            raise VersionPolicyError("无法验证模板 release 来源；仅允许读取历史", "invalid_version_source")
    return None


def locked_scheme(lock: dict) -> tuple[Version, str | None]:
    versions = {(item["version"], item.get("source", {}).get("git", ""))
                for item in lock["package"] if item["name"] == "scheme"}
    if len(versions) != 1:
        raise ValueError("锁文件必须确定实际 Scheme 版本和来源")
    value, git = versions.pop()
    return formal_version(value), git.rsplit("#", 1)[-1] if "#" in git else None


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


def task_files(directory: Path, kind: str) -> tuple[dict, Path, dict]:
    root = (SoloSettings.SHARED_DIR / "runs").resolve()
    directory = directory.resolve()
    if directory == root or not directory.is_relative_to(root):
        raise ValueError("任务目录必须位于对应 runs/{id} 中")
    data = json.loads(file_in_run(directory, "input.json").read_text(encoding="utf-8"))
    if data["kind"] != kind:
        raise ValueError("任务 kind 与数据库记录不一致")
    lockfile = file_in_run(directory, data["environment"]["lockfile"])
    if lockfile.name != "uv.lock":
        raise ValueError("任务锁文件必须为 uv.lock")
    lock = tomllib.loads(lockfile.read_text(encoding="utf-8"))
    if any({"editable", "directory"} & item.get("source", {}).keys() for item in lock["package"]):
        raise ValueError("正式任务不能依赖未冻结的源码目录")
    return data, lockfile, lock


def version_retirement(version) -> str | None:
    try:
        if version.scheme_version:
            return release_reason("scheme", version=version.scheme_version)
        directory = run_path(version.id)
        data = json.loads((directory / "input.json").read_text(encoding="utf-8"))
        _, _, lock = task_files(directory, data["kind"])
        actual, commit = locked_scheme(lock)
        return release_reason("scheme", commit=commit, version=str(actual))
    except (OSError, ValueError, KeyError, TypeError):
        return "成果尚未记录有效的 ActualScheme；仅允许读取历史"


def require_result_series(package_version: str, scheme: Version) -> None:
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
    if Path(value).is_absolute():
        return Path(value).resolve()
    location = urlsplit(value)
    if location.query or location.fragment or location.netloc or location.scheme not in {"", "file"}:
        raise ValueError("锁文件 wheel 来源必须是本地文件")
    path = Path(url2pathname(location.path) if location.scheme == "file" else unquote(location.path))
    return (base / path).resolve()


def locked_local_wheel(directory: Path, lockfile: Path, package: dict) -> Path:
    source = package.get("source", {})
    if "path" in source:
        return file_in_run(directory, str(local_lock_path(lockfile.parent, source["path"])))
    registry = local_lock_path(lockfile.parent, source["registry"])
    if not registry.is_relative_to(directory.resolve()):
        raise ValueError("任务本地 registry 必须位于对应 runs/{id} 中")
    from core.apps.artifacts.service import wheel_metadata
    for entry in package["wheels"]:
        path = local_lock_path(registry if "path" in entry else lockfile.parent, entry.get("path", entry.get("url")))
        path = file_in_run(directory, str(path))
        if not path.is_relative_to(registry):
            raise ValueError("锁定 wheel 必须位于其本地 registry 中")
        name, version, _, _ = wheel_metadata(path)
        if name == canonicalize_name(package["name"]) and version == package["version"]:
            return path
    raise ValueError("本地 registry 没有对应 package/version 的 wheel")


def package_name(project) -> str:
    return canonicalize_name(project.package_name)


def task_components(data: dict) -> dict[str, dict]:
    items = [data["factor"]] if data["kind"] == "factor" else data["algos"].values()
    return {canonicalize_name(item["package"]): item for item in items if item}


def local_task_packages(lock: dict) -> dict[str, dict]:
    return {canonicalize_name(item["name"]): item for item in lock["package"]
            if "path" in item.get("source", {}) or RESEARCH_PACKAGE.fullmatch(canonicalize_name(item["name"]))
            or ("registry" in item.get("source", {}) and (
                Path(item["source"]["registry"]).is_absolute() or urlsplit(item["source"]["registry"]).scheme in {"", "file"}))}


def snapshot_values(value) -> dict[str, dict]:
    return {canonicalize_name(item["package"]): item for item in (value or {}).values()}


def require_accepted_artifacts(accepted_artifacts: dict | None, scheme: Version) -> None:
    """Apply release admission to frozen snapshots without recapturing artifacts."""
    for snapshot in snapshot_values(accepted_artifacts).values():
        if snapshot["kind"] != "dependency":
            reason = source_reason(snapshot["sources"])
            if reason:
                raise VersionPolicyError(reason)
            require_release("scheme", version=snapshot["scheme_version"])
            require_result_series(snapshot["version"], scheme)


def capture_task_artifacts(session, record, *, project=None, directory: Path | None = None,
                           kind: str) -> dict[str, dict]:
    from core.apps.artifacts.service import (artifact_snapshot, register_asset, register_file,
                                            require_active, require_artifact)
    directory = directory or run_path(record.id)
    data, lockfile, lock = task_files(directory, kind)
    actual = require_locked_scheme(lock)
    components = task_components(data)
    accepted = {}
    selections = [project.artifact_dependencies] if project is not None else [
        selection.get("accepted_artifacts") for selection in record.components.values()]
    for selection in selections:
        for name, snapshot in snapshot_values(selection).items():
            accepted.setdefault(name, []).append(snapshot)
    captured = {}
    for name, package in local_task_packages(lock).items():
        wheel = locked_local_wheel(directory, lockfile, package)
        component = components.get(name)
        if component is not None and (component["version"] != package["version"]
                or file_in_run(directory, component["wheel"]) != wheel):
            raise ValueError("输入组件与冻结锁文件不一致")
        if project is not None and name == project.package_name:
            artifact = register_file(session, wheel, project, component=component, scheme_version=str(actual))
        elif name in accepted:
            snapshot = next((item for item in accepted[name] if item["version"] == package["version"]), None)
            if snapshot is None:
                raise ValueError("任务依赖版本与已接受成果不一致")
            artifact = require_artifact(session, snapshot)
            require_active(artifact)
        elif RESEARCH_PACKAGE.fullmatch(name):
            raise ValueError(f"研究依赖尚未接受成果 ID：{name}")
        else:
            artifact = register_asset(session, wheel, scheme_version=str(actual))
        captured[name] = artifact_snapshot(artifact)
    if set(components) - set(captured):
        raise ValueError("输入组件缺少对应的冻结产物")
    return captured


def check_task(session, record, *, project=None, kind: str) -> None:
    """Runtime admission uses database state and release provenance only."""
    expected = project.kind if project is not None else "strategy"
    if kind != expected:
        raise VersionPolicyError("任务 kind 与数据库记录不一致", "task_identity_mismatch")
    if record.status not in {"queued", "running"}:
        raise VersionPolicyError("任务尚未提交或已经结束", "task_not_submitted")
    require_release("scheme", version=record.scheme_version)
    actual = formal_version(record.scheme_version)
    if project is not None:
        require_result_series(record.package_version, actual)
    require_accepted_artifacts(record.accepted_artifacts, actual)
