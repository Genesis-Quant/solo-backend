"""在共享目录创建 uv 环境，注册 Jupyter 能直接发现的 Kernel。"""

import json
import os
from pathlib import Path
import subprocess

from config import SoloSettings


class EnvironmentError(Exception):
    pass


def kernel_directory(project_id: str) -> Path:
    return SoloSettings.HOME_DIR / ".jupyter" / "kernels" / f"solo-{project_id}"


def prepare_environment(directory: Path, project_id: str, name: str) -> None:
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(directory / ".venv"),
        "UV_PYTHON_INSTALL_DIR": str(SoloSettings.HOME_DIR / ".python"),
        "UV_PYTHON_BIN_DIR": "/tmp/solo-python-bin",
        "UV_CACHE_DIR": "/tmp/solo-uv-cache",
        "UV_LINK_MODE": "copy",
        "GIT_TERMINAL_PROMPT": "0",
    }
    try:
        result = subprocess.run(
            ["uv", "sync", "--project", str(directory), "--python", "3.12", "--managed-python"],
            env=env, capture_output=True, text=True, timeout=600,
        )
    except subprocess.TimeoutExpired as error:
        raise EnvironmentError("项目环境安装超时，请检查依赖下载网络后重试") from error
    if result.returncode:
        # 只返回 uv 的最后一段错误，避免将父进程环境或认证信息发给浏览器。
        message = result.stderr[-3000:]
        for secret in (SoloSettings.GITEE_TOKEN, SoloSettings.JUPYTER_TOKEN):
            if secret:
                message = message.replace(secret, "[REDACTED]")
        raise EnvironmentError(f"项目环境安装失败：\n{message}")
    python = directory / ".venv" / "bin" / "python"
    result = subprocess.run([str(python), "-c", "import ipykernel"], capture_output=True, timeout=30)
    if result.returncode:
        raise EnvironmentError("模板的开发依赖缺少 ipykernel，无法注册项目 Kernel")
    kernel = kernel_directory(project_id)
    kernel.mkdir(parents=True, exist_ok=True)
    kernel_name = f"solo-{project_id}"
    (kernel / "kernel.json").write_text(json.dumps({
        "argv": [str(python), "-m", "ipykernel_launcher", "-f", "{connection_file}"],
        "display_name": name,
        "language": "python",
        "env": {"UV_PROJECT_ENVIRONMENT": str(directory / ".venv")},
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    notebook_path = directory / "research.ipynb"
    notebook = json.loads(notebook_path.read_text(encoding="utf-8"))
    notebook.setdefault("metadata", {})["kernelspec"] = {
        "name": kernel_name, "display_name": name, "language": "python",
    }
    notebook_path.write_text(json.dumps(notebook, ensure_ascii=False, indent=2), encoding="utf-8")
