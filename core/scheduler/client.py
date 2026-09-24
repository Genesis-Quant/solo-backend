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
    pass


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
                f"DolphinScheduler 请求失败：{method} {path}"
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
        if kind not in {"factor", "backtest"}:
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
