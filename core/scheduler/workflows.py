"""沿用 Arena：Python Gateway 管理定义，Shell 任务运行 Runtime CLI。"""

from config import DolphinSchedulerSettings as Settings


def sync_workflows() -> dict[str, int]:
    Settings.configure_sdk_environment()
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
    for kind in ("factor", "backtest"):
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
                command=f'exec {Settings.RUNTIME_COMMAND} run --input-file "${{input_file}}"',
                description="读取共享 input.json，在锁定 uv 环境运行，报告写回 /shared/runs",
                worker_group=Settings.WORKER_GROUP,
                fail_retry_times=0,
            )
        codes[kind] = int(workflow.submit())
    return codes
