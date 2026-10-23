"""沿用 Arena：Python Gateway 管理定义，Shell 任务运行 Runtime CLI。"""

import socket
from concurrent.futures import Future, wait
from threading import Event, Thread
from time import monotonic

from config import DolphinSchedulerSettings as Settings
from config import SoloSettings


def wait_for_gateway(timeout: float = 60) -> None:
    """在总期限内等待 Gateway，不重试有副作用的工作流同步。"""
    deadline = monotonic() + timeout
    unavailable = RuntimeError(
        f"DolphinScheduler Gateway 在 {timeout:g} 秒内未就绪："
        f"{Settings.HOST}:{Settings.GATEWAY_PORT}"
    )
    if timeout <= 0:
        raise unavailable

    finished: Future[None] = Future()
    stopped = Event()

    def probe() -> None:
        try:
            while not stopped.is_set() and (remaining := deadline - monotonic()) > 0:
                try:
                    with socket.create_connection(
                        (Settings.HOST, Settings.GATEWAY_PORT), timeout=min(2, remaining)
                    ):
                        return
                except OSError:
                    stopped.wait(min(1, max(0, deadline - monotonic())))
        except Exception as error:
            finished.set_exception(error)
        finally:
            if not finished.done():
                finished.set_result(None)

    # getaddrinfo 无法被 socket timeout 中断；守护线程只探测连接，迟到的 socket 仍会关闭。
    Thread(target=probe, name="gateway-readiness", daemon=True).start()
    try:
        completed, _ = wait((finished,), timeout=max(0, deadline - monotonic()))
        if not completed or monotonic() >= deadline:
            raise unavailable
        finished.result()
    finally:
        stopped.set()


def sync_workflows() -> dict[str, int]:
    Settings.configure_sdk_environment()
    wait_for_gateway()
    # SDK 配置在 import 时读取，必须先设置连接和凭据。
    from pydolphinscheduler.core.workflow import Workflow
    from pydolphinscheduler.models.project import Project
    from pydolphinscheduler.models.user import User
    from pydolphinscheduler.tasks.shell import Shell

    User(
        name=Settings.USERNAME, password=Settings.PASSWORD, tenant=Settings.TENANT
    ).create_if_not_exists()
    Project(name=Settings.PROJECT).create_if_not_exists(Settings.USERNAME)
    codes: dict[str, int] = {}
    for kind in ("factor", "model", "optimize", "control", "execution", "strategy"):
        with Workflow(
            name=kind,
            description=f"Solo {kind} 研究任务",
            user=Settings.USERNAME,
            project=Settings.PROJECT,
            worker_group=Settings.WORKER_GROUP,
            execution_type="PARALLEL",
            release_state="online",
            param={"input_file": ""},
        ) as workflow:
            Shell(
                name=kind,
                command=f'exec {Settings.RUNTIME_COMMAND} apps {kind} --input-file "${{input_file}}"',
                description=(
                    "读取共享 input.json，在锁定 uv 环境运行，"
                    f"报告写回 {SoloSettings.SHARED_DIR.as_posix()}/runs"
                ),
                worker_group=Settings.WORKER_GROUP,
                fail_retry_times=0,
            )
        codes[kind] = int(workflow.submit())
    return codes
