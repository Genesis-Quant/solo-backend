"""DolphinScheduler 3.2 HTTP API；仅重试查询，不重试提交。"""

import json
import re
from pathlib import PurePosixPath
from typing import Any, Self

import requests
from config import DolphinSchedulerSettings as Settings
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class DolphinSchedulerError(RuntimeError):
    def __init__(self, message: str, *, submission_unknown: bool = False) -> None:
        super().__init__(message)
        self.submission_unknown = submission_unknown


def validate_input_file(input_file: str) -> str:
    """DS 会把参数替换进 Shell，因此只接受共享目录内的安全绝对路径。"""
    path = PurePosixPath(input_file)
    if (
        not re.fullmatch(r"/shared/runs/[\w./-]+\.json", input_file)
        or ".." in path.parts
        or str(path) != input_file
    ):
        raise ValueError(
            "input_file 必须是 /shared/runs 下的 JSON 路径，仅允许字母、数字、下划线、点、横线和目录分隔符"
        )
    return input_file


class DolphinSchedulerClient:
    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout
        self.session = requests.Session()
        retry = Retry(
            total=2, allowed_methods={"GET"}, status_forcelist=(502, 503, 504)
        )
        self.session.mount("http://", HTTPAdapter(max_retries=retry))

    def __enter__(self) -> Self:
        try:
            self.request(
                "POST",
                "/login",
                {
                    "userName": Settings.USERNAME,
                    "userPassword": Settings.PASSWORD,
                },
            )
        except Exception:
            self.session.close()
            raise
        return self

    def __exit__(self, *ignored: object) -> None:
        self.session.close()

    def request(
        self, method: str, path: str, params: dict[str, Any] | None = None
    ) -> Any:
        try:
            response = self.session.request(
                method,
                f"{Settings.BASE_URL}{path}",
                timeout=self.timeout,
                **({"params": params} if method == "GET" else {"data": params}),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            # 不输出 URL/body，避免登录密码出现在异常日志。
            raise DolphinSchedulerError(
                f"DolphinScheduler 请求失败：{method} {path}",
                submission_unknown=(
                    method == "POST" and path.endswith("/executors/start-process-instance")
                    and not (
                        isinstance(error, requests.HTTPError)
                        and error.response is not None
                        and 400 <= error.response.status_code < 500
                    )
                ),
            ) from error
        if not payload.get("success", payload.get("code") == 0):
            raise DolphinSchedulerError(
                f"DolphinScheduler：{payload.get('msg', '未知错误')}"
            )
        return payload.get("data")

    def project_code(self) -> int:
        for project in self.request("GET", "/projects/created-and-authed") or []:
            if project["name"] == Settings.PROJECT:
                return int(project["code"])
        raise DolphinSchedulerError(f"项目 {Settings.PROJECT} 尚未初始化")

    def definition(self, project: int, kind: str) -> dict[str, Any]:
        if kind not in {"factor", "model", "optimize", "control", "execution", "strategy"}:
            raise ValueError(f"未知工作流：{kind}")
        result = self.request(
            "GET",
            f"/projects/{project}/process-definition/query-by-name",
            {"name": kind},
        )
        return result.get("processDefinition", result)

    def start(self, kind: str, input_file: str) -> Any:
        input_file = validate_input_file(input_file)
        project = self.project_code()
        definition = self.definition(project, kind)
        return self.request(
            "POST",
            f"/projects/{project}/executors/start-process-instance",
            {
                "processDefinitionCode": definition["code"],
                "scheduleTime": "",
                "failureStrategy": "END",
                "warningType": "NONE",
                "processInstancePriority": "MEDIUM",
                "workerGroup": Settings.WORKER_GROUP,
                "tenantCode": Settings.TENANT,
                "startParams": json.dumps({"input_file": input_file}),
            },
        )

    def instances(self, kind: str, page: int = 1) -> list[dict[str, Any]]:
        project = self.project_code()
        definition = self.definition(project, kind)
        result = self.request(
            "GET",
            f"/projects/{project}/process-instances",
            {
                "processDefineCode": definition["code"],
                "pageNo": page,
                "pageSize": 100,
            },
        )
        return result.get("totalList") or []

    def instance(self, instance_id: int) -> dict[str, Any]:
        project = self.project_code()
        return self.request(
            "GET", f"/projects/{project}/process-instances/{instance_id}"
        )

    def tasks(self, instance_id: int) -> list[dict[str, Any]]:
        project = self.project_code()
        result = self.request(
            "GET", f"/projects/{project}/process-instances/{instance_id}/tasks"
        )
        return result.get("taskList") or []

    def log(self, task_id: int, offset: int = 0, limit: int = 1000) -> dict[str, Any]:
        result = self.request(
            "GET",
            "/log/detail",
            {
                "taskInstanceId": task_id,
                "skipLineNum": offset,
                "limit": limit,
            },
        )
        message = result.get("message", "")
        # 3.2.2 的空日志页也可能返回 lineNum=1。
        lines = int(result.get("lineNum", 0)) if message else 0
        return {"message": message, "next_offset": offset + lines}

    def task_log(
        self,
        *,
        task_instance_id: int,
        skip_line_num: int = 0,
        limit: int = 1000,
    ) -> dict[str, Any]:
        """Read a page of a task log and return the next line cursor."""
        result = self.task_log_detail(task_instance_id, skip_line_num, limit)
        if isinstance(result, dict):
            message = str(result.get("message", ""))
            # DolphinScheduler 3.2.2 reports lineNum=1 even when a request past
            # the end of the log returns an empty message. An empty page must
            # not advance the cursor or polling will invent one line per tick.
            returned_lines = int(result.get("lineNum", 0)) if message else 0
            next_line_num = skip_line_num + returned_lines
            return {
                "skip_line_num": skip_line_num,
                "returned_lines": returned_lines,
                "next_line_num": next_line_num,
                "has_more": bool(
                    message
                    and self.task_log_message(
                        self.task_log_detail(task_instance_id, next_line_num, 1)
                    )
                ),
                "message": message,
            }
        message = str(result or "")
        returned_lines = len(message.splitlines())
        next_line_num = skip_line_num + returned_lines
        return {
            "skip_line_num": skip_line_num,
            "returned_lines": returned_lines,
            "next_line_num": next_line_num,
            "has_more": bool(
                message
                and self.task_log_message(
                    self.task_log_detail(task_instance_id, next_line_num, 1)
                )
            ),
            "message": message,
        }

    def task_log_detail(
        self,
        task_instance_id: int,
        skip_line_num: int,
        limit: int,
    ) -> Any:
        return self.request(
            "GET",
            "/log/detail",
            params={
                "taskInstanceId": task_instance_id,
                "skipLineNum": skip_line_num,
                "limit": limit,
            },
        )

    @staticmethod
    def task_log_message(result: Any) -> str:
        if isinstance(result, dict):
            return str(result.get("message", ""))
        return str(result or "")

    def download_log(self, task_id: int) -> str:
        project = self.project_code()
        try:
            with self.session.get(
                f"{Settings.BASE_URL}/log/{project}/download-log",
                params={"taskInstanceId": task_id}, timeout=self.timeout,
            ) as response:
                response.raise_for_status()
                if "json" in response.headers.get("Content-Type", "").lower():
                    raise DolphinSchedulerError("DolphinScheduler 日志下载失败")
                return response.content.decode("utf-8", errors="replace")
        except requests.RequestException as error:
            raise DolphinSchedulerError("DolphinScheduler 日志下载失败") from error
