"""Delegate project environments to Jupyter, where their absolute paths are valid."""

import json
from pathlib import Path

import requests
from config import SoloSettings


class EnvironmentError(Exception):
    pass


def kernel_directory(project_id: str) -> Path:
    return SoloSettings.SHARED_DIR / "kernels" / f"solo-{project_id}"


def request_environment(directory: Path, project_id: str, name: str, *, locked: bool = False,
                        mode: str = "prepare") -> None:
    try:
        path = directory.resolve().relative_to(SoloSettings.SHARED_DIR.resolve()).as_posix()
    except ValueError as error:
        raise EnvironmentError("项目环境路径不在共享目录内") from error
    headers = {"Authorization": f"token {SoloSettings.JUPYTER_TOKEN}"} if SoloSettings.JUPYTER_TOKEN else {}

    def redact(message: str) -> str:
        for secret in (SoloSettings.GITEE_TOKEN, SoloSettings.JUPYTER_TOKEN):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        return message[-3000:]

    try:
        with requests.Session() as client:
            client.trust_env = False
            response = client.post(
                SoloSettings.JUPYTER_API_URL + "/solo/project/environment",
                headers=headers, json={"path": path, "project_id": project_id, "name": name,
                                       "locked": locked, "mode": mode},
                timeout=(3, 650), allow_redirects=False,
            )
        if response.status_code != 200:
            try:
                body = response.json()
                # Jupyter 把具体原因放在 reason，message 只是 HTTP 状态的通用短语。
                message = body.get("reason") or body.get("message") or "Jupyter 环境服务请求失败"
            except (ValueError, AttributeError):
                message = "Jupyter 环境服务请求失败"
            raise EnvironmentError(f"项目环境安装失败：\n{redact(str(message))}")
    except requests.Timeout:
        raise EnvironmentError("项目环境安装超时，请检查 Jupyter 服务和依赖下载网络后重试") from None
    except requests.RequestException:
        raise EnvironmentError("无法连接 Jupyter 项目环境服务，请稍后重试") from None


def prepare_environment(directory: Path, project_id: str, name: str, *, locked: bool = False) -> None:
    request_environment(directory, project_id, name, locked=locked)


def sync_environment(directory: Path) -> None:
    directory = directory.resolve()
    shared = SoloSettings.SHARED_DIR.resolve()
    if not directory.is_relative_to(shared) or directory == shared:
        raise EnvironmentError("项目环境路径不在共享目录内")
    current = directory
    while current != shared:
        marker = current / ".solo"
        if marker.exists():
            try:
                if not marker.resolve().is_relative_to(shared):
                    raise ValueError("Metadata outside shared directory")
                metadata = json.loads(marker.read_text(encoding="utf-8"))
                project_id, name = metadata["project_id"], metadata["name"]
            except (OSError, ValueError, KeyError, TypeError) as error:
                raise EnvironmentError("项目缺少有效的 .solo 身份") from error
            request_environment(directory, project_id, name, mode="sync")
            return
        current = current.parent
    raise EnvironmentError("项目缺少有效的 .solo 身份")
