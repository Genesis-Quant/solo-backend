"""读取 Gitee Tag，并从对应 commit 归档复制指定模板目录。"""

from io import BytesIO
from base64 import b64decode
from functools import lru_cache
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from tempfile import TemporaryDirectory
from threading import Lock
from time import monotonic
import tomllib
from zipfile import BadZipFile, ZipFile

import requests
import tomlkit
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version, InvalidVersion

from config import SoloSettings
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
        while True:
            data = gitee_get(f"tags?per_page=100&page={page}", repository).json()
            versions.extend(TemplateVersion(tag=item["name"], commit=item["commit"]["sha"]) for item in data)
            if len(data) < 100:
                break
            page += 1
        versions.sort(key=version_key, reverse=True)
        _cache[repository] = (monotonic(), versions)
        return list(versions)


@lru_cache(maxsize=256)
def project_metadata(repository: str, commit: str, path: str) -> dict:
    response = gitee_get(f"contents/{path}?ref={commit}", repository).json()
    return tomllib.loads(b64decode(response["content"]).decode("utf-8"))["project"]


def list_scheme_versions(refresh: bool = False) -> list[TemplateVersion]:
    return [release.model_copy(update={
        "version": project_metadata(SoloSettings.SCHEME_REPOSITORY, release.commit, "pyproject.toml")["version"],
    }) for release in repository_versions(SoloSettings.SCHEME_REPOSITORY, refresh)]


def select_scheme(tag: str) -> TemplateVersion:
    selected = next((v for v in list_scheme_versions() if v.tag == tag), None)
    if selected is None:
        raise ValueError("scheme 版本不存在，请刷新版本列表")
    return selected


def list_versions(kind: ProjectKind, scheme_version: str, refresh: bool = False) -> list[TemplateVersion]:
    selected = select_scheme(scheme_version)
    scheme = Version(project_metadata(SoloSettings.SCHEME_REPOSITORY, selected.commit, "pyproject.toml")["version"])
    result = []
    for release in repository_versions(SoloSettings.TEMPLATE_REPOSITORY, refresh):
        project = project_metadata(SoloSettings.TEMPLATE_REPOSITORY, release.commit, f"{kind}/pyproject.toml")
        try:
            if Version(project["version"]).major != scheme.major:
                continue
        except InvalidVersion:
            continue
        requirements = [Requirement(value) for value in project.get("dependencies", [])]
        required = SpecifierSet(f">={scheme.major}.0.0,<{scheme.major + 1}.0.0")
        scheme_requirements = [r for r in requirements if r.name.lower() == "scheme"]
        if len(scheme_requirements) == 1 and not scheme_requirements[0].url and not scheme_requirements[0].marker and scheme_requirements[0].specifier == required:
            result.append(release)
    return result


def pin_scheme(directory: Path, selected: TemplateVersion) -> None:
    """wheel 声明整个大版本范围；uv source/lock 固定项目实际使用的 commit。"""
    version = project_metadata(SoloSettings.SCHEME_REPOSITORY, selected.commit, "pyproject.toml")["version"]
    path = directory / "pyproject.toml"
    document = tomlkit.parse(path.read_text(encoding="utf-8"))
    dependencies = document["project"].get("dependencies", [])
    document["project"]["dependencies"] = [
        value for value in dependencies if Requirement(value).name.lower() != "scheme"
    ] + [f"scheme>={Version(version).major}.0.0,<{Version(version).major + 1}.0.0"]
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
    # 每个研究项目有独立包名和模块名，可同时安装多个同类成果。
    for file in destination.rglob("*"):
        if file.is_file() and file.suffix in {".py", ".toml", ".ipynb", ".md"}:
            text = file.read_text(encoding="utf-8")
            text = re.sub(rf"\b(from\s+|import\s+){re.escape(old_module)}(?=[.\s,]|$)",
                          lambda match: match[1] + new_module, text)
            file.write_text(text, encoding="utf-8")
    document = tomlkit.parse(pyproject.read_text(encoding="utf-8"))
    document["project"]["name"] = new_package
    pyproject.write_text(tomlkit.dumps(document), encoding="utf-8")
    module = destination / "src" / old_module
    if module.is_dir():
        module.rename(module.with_name(new_module))
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
