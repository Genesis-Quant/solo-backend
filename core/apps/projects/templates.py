"""读取 Gitee Tag，并从对应 commit 归档复制指定模板目录。"""

import json
import os
import re
import stat
import subprocess
import tomllib
from base64 import b64decode
from functools import lru_cache
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
from zipfile import BadZipFile, ZipFile

import requests
import tomlkit
from packaging.requirements import Requirement
from packaging.version import Version

from config import SoloSettings
from core.apps import version_policy as policy

from .schemas import ProjectKind, TemplateVersion


class TemplateError(Exception):
    pass


_cache: dict[str, tuple[float, list[TemplateVersion]]] = {}
_lock = Lock()


def gitee_get(path: str, repository: str | None = None) -> requests.Response:
    headers = {"Accept": "application/json"}
    if SoloSettings.GITEE_TOKEN:
        headers["Authorization"] = f"Bearer {SoloSettings.GITEE_TOKEN}"
    try:
        response = requests.get(
            f"https://gitee.com/api/v5/repos/{repository or SoloSettings.TEMPLATE_REPOSITORY}/{path}",
            headers=headers, timeout=(10, 45),
        )
        response.raise_for_status()
        return response
    except requests.RequestException as error:
        raise TemplateError("无法读取 Gitee 模板仓库，请稍后重试或检查网络及 Gitee 访问额度") from error


def git_output(*arguments: str) -> bytes:
    return subprocess.run(
        ["git", *arguments], check=True, capture_output=True, timeout=120,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    ).stdout


def git_versions(repository: str) -> list[TemplateVersion]:
    """REST API 配额不足时，经 Git 协议读取同一组 Tag 及其提交。"""
    try:
        output = git_output("ls-remote", "--tags", f"https://gitee.com/{repository}.git").decode("utf-8")
    except (OSError, subprocess.SubprocessError, UnicodeError) as error:
        raise TemplateError("无法读取 Gitee 模板仓库，请稍后重试或检查网络") from error
    commits: dict[str, str] = {}
    for line in output.splitlines():
        commit, _, reference = line.partition("\t")
        tag = reference.removeprefix("refs/tags/")
        if tag.endswith("^{}"):
            commits[tag.removesuffix("^{}")] = commit  # 附注 Tag 取其指向的提交。
        else:
            commits.setdefault(tag, commit)
    return [TemplateVersion(tag=tag, commit=commit) for tag, commit in commits.items()]


def git_file(repository: str, commit: str, path: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise TemplateError("模板 commit 无效")
    try:
        with TemporaryDirectory(prefix="solo-template-") as temporary:
            git_output("-C", temporary, "init", "--bare")
            git_output("-C", temporary, "fetch", "--depth=1", "--no-tags",
                       f"https://gitee.com/{repository}.git", commit)
            return git_output("-C", temporary, "show", f"FETCH_HEAD:{path}")
    except (OSError, subprocess.SubprocessError) as error:
        raise TemplateError("无法从 Gitee 读取版本文件，请检查网络后重试") from error


def version_key(version: TemplateVersion) -> tuple[int, int, int, int, bool, str]:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)(.*)", version.tag)
    if match:
        return (1, int(match[1]), int(match[2]), int(match[3]), not bool(match[4]), version.tag)
    return (0, 0, 0, 0, False, version.tag)


def repository_versions(repository: str, refresh: bool = False) -> list[TemplateVersion]:
    with _lock:
        cached = _cache.get(repository, (0, []))
        if not refresh and monotonic() - cached[0] < 60:
            return list(cached[1])
        versions: list[TemplateVersion] = []
        page = 1
        try:
            while True:
                data = gitee_get(f"tags?per_page=100&page={page}", repository).json()
                versions.extend(TemplateVersion(tag=item["name"], commit=item["commit"]["sha"]) for item in data)
                if len(data) < 100:
                    break
                page += 1
        except TemplateError:
            versions = git_versions(repository)
        versions.sort(key=version_key, reverse=True)
        _cache[repository] = (monotonic(), versions)
        return list(versions)


@lru_cache(maxsize=256)
def project_metadata(repository: str, commit: str, path: str) -> dict:
    try:
        content = b64decode(gitee_get(f"contents/{path}?ref={commit}", repository).json()["content"])
    except TemplateError:
        content = git_file(repository, commit, path)
    return tomllib.loads(content.decode("utf-8"))["project"]


def list_scheme_versions(refresh: bool = False) -> list[TemplateVersion]:
    result = []
    for release in repository_versions(SoloSettings.SCHEME_REPOSITORY, refresh):
        # Known retired identities need no additional remote metadata request.
        if policy.retired_release("scheme", tag=release.tag, commit=release.commit):
            continue
        actual = project_metadata(SoloSettings.SCHEME_REPOSITORY, release.commit, "pyproject.toml")["version"]
        try:
            policy.require_release("scheme", tag=release.tag, commit=release.commit, version=actual)
        except policy.VersionPolicyError:
            continue
        result.append(release.model_copy(update={"version": actual}))
    return result


def select_scheme(tag: str) -> TemplateVersion:
    retired = policy.retired_release("scheme", tag=tag)
    if retired:
        raise policy.VersionPolicyError(retired["reason"])
    selected = next((v for v in list_scheme_versions() if v.tag == tag), None)
    if selected is None:
        raise ValueError("scheme 版本不存在或已退役，请刷新版本列表")
    policy.require_release("scheme", tag=selected.tag, commit=selected.commit, version=selected.version)
    return selected


def validate_template(project: dict, scheme: Version) -> None:
    """模板主、次版本与 Scheme 一致，依赖允许该系列的任意 patch。"""
    version = Version(project.get("version", ""))
    if not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", str(value)) for value in (version, scheme)):
        raise ValueError("模板与 scheme 必须使用正式版本（主、次版本一致，patch 任意）")
    if (version.major, version.minor) != (scheme.major, scheme.minor):
        raise ValueError("模板主、次版本必须与 scheme 一致")
    requirements = [(text, Requirement(text)) for text in project.get("dependencies", [])]
    scheme_requirements = [(text, r) for text, r in requirements if r.name.lower() == "scheme"]
    minimum = Version(f"{scheme.major}.{scheme.minor}.0")
    maximum = Version(f"{scheme.major}.{scheme.minor + 1}.0")
    message = f"模板必须声明同一 scheme 主、次版本的唯一完整 patch 范围：>={minimum},<{maximum}（无 URL、marker、extras）"
    if len(scheme_requirements) != 1:
        raise ValueError(message)
    text, requirement = scheme_requirements[0]
    bounds = {specifier.operator: specifier.version for specifier in requirement.specifier}
    # SpecifierSet 会去重；原始声明也只能包含两个边界。
    if (requirement.url or requirement.marker or requirement.extras or text.count(",") != 1
            or len(requirement.specifier) != 2 or set(bounds) != {">=", "<"}
            or not all(re.fullmatch(r"[0-9]+(?:\.[0-9]+){0,2}", value) for value in bounds.values())
            or Version(bounds[">="]) != minimum or Version(bounds["<"]) != maximum):
        raise ValueError(message)
    if scheme not in requirement.specifier:
        raise ValueError(f"所选 scheme {scheme} 不满足模板依赖：{requirement}")


def list_versions(kind: ProjectKind, scheme_version: str, refresh: bool = False) -> list[TemplateVersion]:
    selected = select_scheme(scheme_version)
    actual = selected.version or project_metadata(SoloSettings.SCHEME_REPOSITORY, selected.commit, "pyproject.toml")["version"]
    policy.require_release("scheme", tag=selected.tag, commit=selected.commit, version=actual)
    scheme = Version(actual)
    result = []
    for release in repository_versions(SoloSettings.TEMPLATE_REPOSITORY, refresh):
        if policy.retired_release("template", tag=release.tag, commit=release.commit):
            continue
        project = project_metadata(SoloSettings.TEMPLATE_REPOSITORY, release.commit, f"{kind}/pyproject.toml")
        try:
            policy.require_release("template", tag=release.tag, commit=release.commit, version=project["version"])
            validate_template(project, scheme)
        except ValueError:
            continue
        result.append(release.model_copy(update={"version": project["version"]}))
    return result


def pin_scheme(directory: Path, selected: TemplateVersion) -> None:
    """保留模板依赖范围；验证兼容后仅用 uv source 固定实际 commit。"""
    policy.require_release("scheme", tag=selected.tag, commit=selected.commit, version=selected.version)
    actual = selected.version or project_metadata(SoloSettings.SCHEME_REPOSITORY, selected.commit, "pyproject.toml")["version"]
    policy.require_release("scheme", tag=selected.tag, commit=selected.commit, version=actual)
    version = Version(actual)
    path = directory / "pyproject.toml"
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    policy.require_release("template", version=str(document["project"]["version"]))
    validate_template(document["project"], version)
    sources = document.setdefault("tool", {}).setdefault("uv", {}).setdefault("sources", {})
    sources["scheme"] = {"git": f"https://gitee.com/{SoloSettings.SCHEME_REPOSITORY}", "rev": selected.commit}
    path.write_text(tomlkit.dumps(document), encoding="utf-8")


def unpack_template(content: bytes, kind: ProjectKind, destination: Path, package_suffix: str) -> None:
    """只写入模板内普通文件；目录外路径和链接不予解包。"""
    try:
        with ZipFile(BytesIO(content)) as archive:
            members = []
            for member in archive.infolist():
                parts = PurePosixPath(member.filename).parts
                if len(parts) < 3 or parts[1] != kind or member.is_dir():
                    continue
                if ".." in parts or "\\" in member.filename or PurePosixPath(member.filename).is_absolute():
                    raise TemplateError("模板包含非法文件路径")
                if stat.S_ISLNK(member.external_attr >> 16):
                    raise TemplateError("模板目录不能包含符号链接")
                members.append((member, Path(*parts[2:])))
            if sum(member.file_size for member, _ in members) > 64 * 1024 * 1024:
                raise TemplateError("模板目录超过大小限制")
            for member, relative in members:
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(member))
    except BadZipFile as error:
        raise TemplateError("模板归档无效") from error
    pyproject = destination / "pyproject.toml"
    notebook = destination / "research.ipynb"
    if not pyproject.is_file() or not notebook.is_file():
        raise TemplateError(f"所选版本缺少 {kind} 模板或 research.ipynb")
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    old_package = data["project"]["name"]
    old_module = old_package.replace("-", "_")
    new_package = f"{kind}-{package_suffix}"
    new_module = new_package.replace("-", "_")
    module_import = re.compile(rf"\b(from\s+|import\s+)({re.escape(old_module)})(?=[.\s,]|$)")

    def rename_imports(source: str | list[str]) -> str | list[str]:
        text = source if isinstance(source, str) else "".join(source)
        renamed = module_import.sub(lambda match: match[1] + new_module, text)
        if isinstance(source, str):
            return renamed
        # 在连接后的源码中匹配跨段 import，再映射原边界；不拆散行或丢失空段。
        matches = iter(module_import.finditer(text))
        match = next(matches, None)
        result = []
        offset = delta = previous = 0
        for part in source:
            offset += len(part)
            while match is not None and match.end() <= offset:
                delta += len(new_module) - len(old_module)
                match = next(matches, None)
            boundary = offset + delta
            if match is not None and match.start(2) < offset:
                boundary -= max(0, offset - match.start(2) - len(new_module))
            result.append(renamed[previous:boundary])
            previous = boundary
        return result

    # 每个研究项目有独立包名和模块名，可同时安装多个同类成果。
    for file in destination.rglob("*"):
        if file.is_file() and file.suffix in {".py", ".toml", ".ipynb", ".md"}:
            text = file.read_text(encoding="utf-8")
            if file.suffix == ".ipynb":
                notebook_data = json.loads(text)
                for cell in notebook_data.get("cells", []):
                    if cell.get("cell_type") == "code" and isinstance(cell.get("source"), (str, list)):
                        cell["source"] = rename_imports(cell["source"])
                text = json.dumps(notebook_data, ensure_ascii=False, indent=2)
            else:
                text = rename_imports(text)
            file.write_text(text, encoding="utf-8")
    document = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    document["project"]["name"] = new_package
    pyproject.write_text(tomlkit.dumps(document), encoding="utf-8")
    module = destination / "src" / old_module
    if module.is_dir():
        module.rename(module.with_name(new_module))
    notebook_data = json.loads(notebook.read_text(encoding="utf-8"))
    notebook_data.setdefault("nbformat", 4)
    notebook_data.setdefault("nbformat_minor", 5)
    notebook.write_text(json.dumps(notebook_data, ensure_ascii=False, indent=2), encoding="utf-8")
    (destination / ".gitignore").write_text(".venv/\n__pycache__/\n*.py[cod]\n.ipynb_checkpoints/\ndist/\n.env\n", encoding="utf-8")


def create_directory(kind: ProjectKind, commit: str, destination: Path, package_suffix: str) -> None:
    # Gitee 的 zipball API 即便访问公开仓库也要求认证；Git 按 commit 拉取不需要 token。
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise TemplateError("模板 commit 无效")
    try:
        with TemporaryDirectory(prefix="solo-template-") as temporary:
            def git(*arguments: str) -> bytes:
                return subprocess.run(
                    ["git", "-C", temporary, *arguments],
                    check=True, capture_output=True, timeout=120,
                ).stdout

            git("init", "--bare")
            git("fetch", "--depth=1", "--no-tags",
                f"https://gitee.com/{SoloSettings.TEMPLATE_REPOSITORY}.git", commit)
            content = git("archive", "--format=zip", "--prefix=template/", "FETCH_HEAD", kind)
    except (OSError, subprocess.SubprocessError) as error:
        raise TemplateError("无法从 Gitee 获取模板，请检查网络后重试") from error
    unpack_template(content, kind, destination, package_suffix)
