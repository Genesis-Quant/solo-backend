"""研究与策略共用的运行状态、控制、计时和日志归属。"""

import json
import os
import re
import shutil
from contextlib import contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session, object_session

from config import DolphinSchedulerSettings, SoloSettings
from core.apps import version_policy as policy
from core.apps.projects.models import Project, ProjectVersion
from core.apps.strategies.models import Strategy
from core.scheduler.client import (
    DolphinSchedulerClient,
    DolphinSchedulerError,
    DolphinSchedulerStateError,
    instance_input_file,
    worker_input_file,
)

from core.scheduler.retry_commands import inspect_retry, submit_retry

from .models import TaskAttempt

ACTIVE = frozenset({"building", "queued", "running", "retrying", "stopping"})
TERMINAL = frozenset({"success", "failed", "stopped", "submit_failed"})
SCHEDULER_TERMINAL = frozenset({"SUCCESS", "FAILURE", "STOP", "KILL", "PAUSE", "BLOCK", "FORCED_SUCCESS"})


def scheduler_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(DolphinSchedulerSettings.TIME_ZONE))
    return parsed.astimezone(UTC)


def seconds(start: datetime | None, end: datetime | None = None) -> float:
    if start is None:
        return 0.0
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    if end is not None and end.tzinfo is None:
        end = end.replace(tzinfo=UTC)
    return max(0.0, ((end or datetime.now(UTC)) - start).total_seconds())


def run_directory(record) -> Path:
    return SoloSettings.SHARED_DIR / "runs" / str(record.id)


def current_attempt(session: Session, record, kind: str) -> TaskAttempt:
    for pending in session.new:
        if isinstance(pending, TaskAttempt) and pending.run_id == record.id and pending.number == record.attempt:
            return pending
    attempt = session.scalar(select(TaskAttempt).where(
        TaskAttempt.run_id == record.id, TaskAttempt.number == record.attempt,
    ))
    if attempt is None:
        attempt = TaskAttempt(
            run_id=record.id, number=record.attempt, kind=kind, status=record.status,
            workflow_id=record.workflow_id, scheduler_run_times=record.scheduler_run_times,
            baseline_task_id=0, tasks=[], error=record.error or "", sync_error="",
            created_at=record.created_at, finished_at=None,
        )
        session.add(attempt)
        session.flush()
    return attempt


def owned_instance(client: DolphinSchedulerClient, record, kind: str) -> dict | None:
    expected = worker_input_file(str(run_directory(record) / "input.json"))
    if not record.workflow_id:
        for page in range(1, 101):
            instances = client.instances(kind, page)
            match = next((item for item in instances if instance_input_file(item) == expected), None)
            if match is not None:
                record.workflow_id = int(match["id"])
                break
            if len(instances) < 100:
                return None
    if not record.workflow_id:
        return None
    instance = client.instance(record.workflow_id)
    project = client.project_code()
    definition = client.definition(project, kind)
    if (
        instance.get("id") != record.workflow_id
        or instance.get("projectCode") != project
        or instance.get("processDefinitionCode") != int(definition["code"])
        or instance_input_file(instance) != expected
    ):
        raise DolphinSchedulerError("调度实例与任务冻结输入不一致，未执行操作")
    return instance


def latest_tasks(tasks: list[dict]) -> list[dict]:
    latest = {}
    for task in tasks:
        if type(task.get("id")) is not int:
            continue
        key = task.get("taskCode") or task.get("name") or task["id"]
        if key not in latest or task["id"] > latest[key]["id"]:
            latest[key] = task
    return sorted(latest.values(), key=lambda item: item["id"])


def task_information(task: dict) -> dict:
    started = scheduler_time(task.get("startTime"))
    finished = scheduler_time(task.get("endTime"))
    return {
        "task_instance_id": task["id"], "task_code": task.get("taskCode"),
        "name": task.get("name") or "研究任务", "state": task.get("state") or "WAIT_TO_RUN",
        "host": task.get("host"), "started_at": started.isoformat() if started else None,
        "finished_at": finished.isoformat() if finished else None,
        "duration_seconds": seconds(started, finished),
    }


def hydrate_history(session: Session, record, kind: str, instance: dict, history: list[dict]) -> None:
    run_times = instance.get("runTimes")
    if type(run_times) is not int or run_times < 1:
        raise DolphinSchedulerError("调度器运行次数无效")
    groups = {}
    for task in history:
        groups.setdefault(task.get("taskCode") or task.get("name"), []).append(task)
    if len(groups) != 1:
        return
    tasks = sorted(next(iter(groups.values())), key=lambda item: item["id"])
    if len(tasks) != run_times:
        return
    existing = {item.number: item for item in session.scalars(select(TaskAttempt).where(TaskAttempt.run_id == record.id))}
    for number, task in enumerate(tasks, 1):
        attempt = existing.get(number)
        if attempt is None:
            attempt = TaskAttempt(run_id=record.id, number=number, kind=kind, status="queued", tasks=[], error="", sync_error="", baseline_task_id=0, created_at=record.created_at)
            session.add(attempt)
        attempt.workflow_id = instance["id"]
        attempt.scheduler_run_times = number
        attempt.tasks = [task_information(task)]
        attempt.started_at = scheduler_time(task.get("startTime"))
        attempt.finished_at = scheduler_time(task.get("endTime"))
        attempt.created_at = scheduler_time(task.get("firstSubmitTime") or task.get("submitTime")) or record.created_at
        attempt.scheduler_state = str(task.get("state") or "WAIT_TO_RUN")
        attempt.status = "stopped" if attempt.scheduler_state in {"STOP", "KILL"} else "failed" if attempt.scheduler_state in {"FAILURE", "PAUSE", "BLOCK"} else "success" if attempt.scheduler_state in {"SUCCESS", "FORCED_SUCCESS"} else "running"
        attempt.error = "任务已终止" if attempt.status == "stopped" else "本轮执行失败，请查看日志" if attempt.status == "failed" else ""
        attempt.synced_at = datetime.now(UTC)
    for number, attempt in existing.items():
        if number > run_times:
            session.delete(attempt)
    record.attempt = run_times
    record.scheduler_run_times = run_times
    session.flush()


def apply_record(record, attempt: TaskAttempt) -> None:
    record.status = attempt.status
    record.error = attempt.error
    record.workflow_id = attempt.workflow_id
    record.scheduler_run_times = attempt.scheduler_run_times
    record.finished_at = attempt.finished_at


def retry_visible(attempt: TaskAttempt, instance: dict, tasks: list[dict]) -> bool:
    run_times = instance.get("runTimes")
    return (
        type(run_times) is int and attempt.scheduler_run_times is not None
        and run_times >= attempt.scheduler_run_times
        and (any(task["id"] > attempt.baseline_task_id for task in tasks)
             or instance.get("state") in SCHEDULER_TERMINAL)
    )


def close_unstarted_retry(record, attempt: TaskAttempt, message: str) -> None:
    attempt.retry_state = "rejected"
    attempt.status = "stopped" if attempt.control_action == "stop" else "submit_failed"
    attempt.scheduler_state = "STOP" if attempt.status == "stopped" else "SUBMIT_FAILED"
    attempt.error = message
    attempt.tasks = []
    attempt.started_at = None
    attempt.finished_at = datetime.now(UTC)
    attempt.control_action = None
    apply_record(record, attempt)


def reconcile_retry(session: Session, record, attempt: TaskAttempt, instance: dict, tasks: list[dict], *, guarded: bool) -> bool:
    if attempt.retry_state not in {"prepared", "unknown", "accepted", "legacy"}:
        return False
    if retry_visible(attempt, instance, tasks):
        attempt.retry_state = "visible"
        return False
    legacy = attempt.retry_state == "legacy"
    with (nullcontext() if guarded else task_guard(session, record.id)):
        project = instance["projectCode"]
        definition = instance["processDefinitionCode"]
        result = inspect_retry(
            instance["id"], project, definition, str(run_directory(record) / "input.json"),
            attempt.scheduler_run_times - 1, None if legacy else attempt.id,
        )
        if result.state == "legacy_absent":
            attempt.sync_error = "旧版 HTTP 重试没有可核对的命令凭据；未确认未接受前不能自动重发"
        elif result.state == "pending":
            attempt.retry_state = "legacy" if legacy else "accepted"
            attempt.error = "等待调度器创建本轮 Task" if attempt.control_action != "stop" else "待终止：等待本轮 Task 创建后终止"
        elif result.state == "started":
            attempt.retry_state = "legacy" if legacy else "accepted"
            attempt.error = "本轮已被调度器接受，等待 Task 同步"
        else:
            close_unstarted_retry(record, attempt, "重试命令未被接受或已被调度器拒绝，可重新重试")
    return True


def synchronize(session: Session, record, kind: str, *, force: bool = False, guarded: bool = False) -> TaskAttempt:
    if object_session(record) is session:
        session.refresh(record, with_for_update=True)
    attempt = current_attempt(session, record, kind)
    if attempt.retry_state == "rejected":
        return attempt
    if (
        not force and attempt.synced_at and not attempt.sync_error
        and attempt.status == record.status and attempt.workflow_id == record.workflow_id
        and (attempt.status in TERMINAL or seconds(attempt.synced_at) < 2)
    ):
        return attempt
    if not record.workflow_id and record.status in {"building", "failed", "stopped", "submit_failed"}:
        attempt.status, attempt.error = record.status, record.error or ""
        attempt.finished_at = record.finished_at
        return attempt
    try:
        with DolphinSchedulerClient() as client:
            instance = owned_instance(client, record, kind)
            if instance is None:
                attempt.synced_at = datetime.now(UTC)
                return attempt
            tasks = latest_tasks(client.tasks(instance["id"]))
            if attempt.synced_at is None and attempt.control_action is None:
                history = client.task_history(instance["id"])
                hydrate_history(session, record, kind, instance, history)
                attempt = current_attempt(session, record, kind)
    except DolphinSchedulerError as error:
        attempt.sync_error = str(error)
        attempt.synced_at = datetime.now(UTC)
        return attempt
    attempt.sync_error = ""
    attempt.workflow_id = instance["id"]
    run_times = instance.get("runTimes")
    if attempt.status == "retrying" and attempt.retry_state is None:
        attempt.retry_state = "legacy"
    try:
        if attempt.retry_state in {"prepared", "unknown", "accepted", "legacy"}:
            if reconcile_retry(session, record, attempt, instance, tasks, guarded=guarded):
                attempt.synced_at = datetime.now(UTC)
                session.flush()
                return attempt
    except (DolphinSchedulerError, HTTPException) as error:
        attempt.sync_error = str(error.detail) if isinstance(error, HTTPException) else str(error)
        attempt.synced_at = datetime.now(UTC)
        return attempt
    state = str(instance.get("state") or "WAIT_TO_RUN")
    if attempt.control_action == "stop" and state not in SCHEDULER_TERMINAL:
        try:
            with (nullcontext() if guarded else task_guard(session, record.id)):
                with DolphinSchedulerClient() as client:
                    client.control(kind, record.workflow_id, str(run_directory(record) / "input.json"), "stop",
                                   expected_run_times=attempt.scheduler_run_times)
        except (DolphinSchedulerError, HTTPException) as error:
            attempt.sync_error = str(error.detail) if isinstance(error, HTTPException) else str(error)
            attempt.synced_at = datetime.now(UTC)
            return attempt
    attempt.scheduler_state = state
    attempt.scheduler_run_times = run_times if type(run_times) is int else None
    if attempt.retry_state == "visible":
        tasks = [task for task in tasks if task["id"] > attempt.baseline_task_id]
    attempt.tasks = [task_information(task) for task in tasks]
    starts = [scheduler_time(task.get("startTime")) for task in tasks if task.get("startTime")]
    fallback_start = instance.get("restartTime") if attempt.retry_state == "visible" else instance.get("restartTime") or instance.get("startTime")
    attempt.started_at = min(starts) if starts else scheduler_time(fallback_start)
    attempt.finished_at = scheduler_time(instance.get("endTime")) if state in SCHEDULER_TERMINAL else None
    attempt.error = ""
    if state in {"SUCCESS", "FORCED_SUCCESS"}:
        try:
            data = json.loads((run_directory(record) / "report/run.json").read_text(encoding="utf-8"))
            if data.get("status") != "success" or data["versions"]["scheme"] != record.scheme_version:
                raise ValueError("报告清单与任务实际 Scheme 不一致")
            attempt.status = "success"
            # 研究进程发布报告后自行删除检查点；进程在删除前退出时由这里补删。
            shutil.rmtree(run_directory(record) / "checkpoints", ignore_errors=True)
        except (OSError, ValueError, KeyError, TypeError) as error:
            attempt.status, attempt.error = "failed", f"报告校验失败：{error}"
    elif state in {"STOP", "KILL"}:
        attempt.status, attempt.error = "stopped", "任务已终止"
    elif state in {"FAILURE", "PAUSE", "BLOCK"}:
        attempt.status, attempt.error = "failed", f"DolphinScheduler：{state}，请查看本轮日志"
    elif state == "READY_STOP" or attempt.control_action == "stop":
        attempt.status = "stopping"
    elif state in {"SUBMITTED_SUCCESS", "SERIAL_WAIT", "DELAY_EXECUTION", "WAIT_TO_RUN"}:
        attempt.status = "queued"
    else:
        attempt.status = "running"
    if attempt.status in TERMINAL:
        attempt.control_action = None
    attempt.synced_at = datetime.now(UTC)
    apply_record(record, attempt)
    session.flush()
    return attempt


def duration(attempt: TaskAttempt) -> float:
    if attempt.status in {"building", "queued", "retrying"} or attempt.started_at is None:
        return 0.0
    return seconds(attempt.started_at, attempt.finished_at)


def summary(attempt: TaskAttempt) -> dict:
    return {
        "id": str(attempt.id), "number": attempt.number, "status": attempt.status,
        "workflowId": attempt.workflow_id, "schedulerState": attempt.scheduler_state,
        "createdAt": attempt.created_at.isoformat(),
        "startedAt": attempt.started_at.isoformat() if attempt.started_at else None,
        "finishedAt": attempt.finished_at.isoformat() if attempt.finished_at else None,
        "durationSeconds": duration(attempt), "error": attempt.error, "syncError": attempt.sync_error,
        "tasks": [{**task, "duration_seconds": seconds(scheduler_time(task.get("started_at")), scheduler_time(task.get("finished_at")))} for task in attempt.tasks],
    }


def record_summary(session: Session, record, kind: str) -> dict:
    attempt = current_attempt(session, record, kind)
    if attempt.synced_at is None and not record.workflow_id:
        attempt.status, attempt.error = record.status, record.error or ""
        attempt.finished_at = record.finished_at
    return {
        "attempt": attempt.number, "startedAt": summary(attempt)["startedAt"],
        "finishedAt": summary(attempt)["finishedAt"], "durationSeconds": duration(attempt),
        "duration": f"{int(duration(attempt))}s", "syncError": attempt.sync_error,
        "canStop": attempt.status in {"building", "queued", "running", "retrying", "stopping"},
        "canRetry": attempt.status in {"failed", "stopped", "submit_failed"}
        and bool(record.workflow_id and record.accepted_artifacts)
        and not policy.version_retirement(record),
    }


def require_retry(record, kind: str) -> None:
    directory = run_directory(record)
    try:
        data, _, lock = policy.task_files(directory, kind)
        actual = policy.require_locked_scheme(lock)
        if str(actual) != record.scheme_version:
            raise ValueError("任务 ActualScheme 与冻结环境不一致")
        policy.require_accepted_artifacts(record.accepted_artifacts, actual)
        if not record.accepted_artifacts:
            raise ValueError("任务尚未冻结依赖，请重新保存版本")
        output = (directory / data["output"]).resolve()
        if output == directory.resolve() or not output.is_relative_to(directory.resolve()):
            raise ValueError("报告目录不在任务目录内")
        if (output / "run.json").exists():
            raise ValueError("任务已有成功报告，不能覆盖重试")
    except policy.VersionPolicyError as error:
        raise HTTPException(422, error.detail) from error
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise HTTPException(422, str(error)) from error


@contextmanager
def task_guard(session: Session, run_id: UUID):
    bind = session.get_bind()
    if bind.dialect.name != "postgresql":
        yield
        return
    key = -(run_id.int & ((1 << 62) - 1)) - 1
    with bind.connect() as connection:
        try:
            acquired = bool(connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key}))
            connection.commit()
        except Exception:
            connection.invalidate()
            raise
        if not acquired:
            raise HTTPException(409, "任务正在执行控制操作，请稍后刷新")
        try:
            yield
        finally:
            try:
                connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
                connection.commit()
            except Exception:
                connection.invalidate()
                raise


def control(session: Session, record, kind: str, action: str, expected_attempt: int) -> TaskAttempt:
    with task_guard(session, record.id):
        return execute_control(session, record, kind, action, expected_attempt)


def execute_control(session: Session, record, kind: str, action: str, expected_attempt: int) -> TaskAttempt:
    attempt = synchronize(session, record, kind, force=True, guarded=True)
    if attempt.sync_error:
        raise HTTPException(502, attempt.sync_error)
    if expected_attempt != attempt.number:
        raise HTTPException(409, "任务运行轮次已变化，请刷新后确认")
    if action == "stop" and attempt.status in TERMINAL:
        return attempt
    if action == "stop" and attempt.status == "building":
        attempt.status, attempt.error = "stopped", "构建已取消，不再提交调度器；本地打包完成后可重新保存"
        attempt.finished_at = datetime.now(UTC)
        apply_record(record, attempt)
        session.commit()
        return attempt
    if action == "retry":
        if attempt.status not in {"failed", "stopped", "submit_failed"}:
            raise HTTPException(409, "只能重试失败或已终止的任务")
        require_retry(record, kind)
    elif action != "stop":
        raise HTTPException(422, "未知任务操作")
    if not record.workflow_id:
        raise HTTPException(409, "调度实例尚未就绪，请刷新后重试")
    if action == "stop":
        attempt.status, attempt.control_action = "stopping", "stop"
        attempt.control_requested_at = datetime.now(UTC)
        attempt.error = "正在终止；本轮尚未启动时保留终止意图"
        apply_record(record, attempt)
        session.commit()
        result = synchronize(session, record, kind, force=True, guarded=True)
        session.commit()
        return result
    prepared = None
    try:
        with DolphinSchedulerClient() as client:
            instance = owned_instance(client, record, kind)
            tasks = latest_tasks(client.tasks(record.workflow_id))
        run_times = instance.get("runTimes")
        if type(run_times) is not int or run_times < 1:
            raise DolphinSchedulerError("调度运行次数无效，未提交重试")
        prepared = TaskAttempt(
            run_id=record.id, number=attempt.number + 1, kind=kind, status="retrying",
            workflow_id=record.workflow_id, scheduler_run_times=run_times + 1,
            baseline_task_id=max((task["id"] for task in tasks), default=0),
            tasks=[], error="", sync_error="", control_action="retry", retry_state="prepared",
            scheduler_state="WAIT_TO_RUN", created_at=datetime.now(UTC),
            control_requested_at=datetime.now(UTC),
        )
        session.add(prepared)
        record.attempt = prepared.number
        apply_record(record, prepared)
        session.commit()
        receipt = submit_retry(
            record.workflow_id, instance["projectCode"], instance["processDefinitionCode"],
            str(run_directory(record) / "input.json"), run_times, prepared.id,
        )
        prepared.retry_state = "accepted" if receipt.state in {"pending", "started"} else "rejected"
        if receipt.state in {"absent", "rejected"}:
            close_unstarted_retry(record, prepared, "重试命令未被接受，可重新重试")
        attempt = prepared
        session.commit()
    except DolphinSchedulerError as error:
        if prepared is not None and error.submission_unknown:
            prepared.retry_state = "unknown"
            prepared.error = "重试提交响应未确认，正在核对持久命令；不会盲目重发"
            apply_record(record, prepared)
            session.commit()
            return prepared
        if prepared is not None:
            session.delete(prepared)
            record.attempt = attempt.number
            apply_record(record, attempt)
            session.commit()
        raise HTTPException(409 if isinstance(error, DolphinSchedulerStateError) else 502, str(error)) from error
    result = synchronize(session, record, kind, force=True, guarded=True)
    session.commit()
    return result


def resolve_record(session: Session, run_id: UUID, *, lock: bool = False):
    for model in (ProjectVersion, Strategy):
        query = select(model).where(model.id == run_id)
        if lock:
            query = query.with_for_update()
        record = session.scalar(query.execution_options(populate_existing=True))
        if record is None:
            continue
        project = session.get(Project, record.project_id) if isinstance(record, ProjectVersion) else None
        if isinstance(record, ProjectVersion) and (project is None or project.archived):
            raise HTTPException(404, "任务所属项目不存在")
        kind = project.kind if project else "strategy"
        return record, project, kind
    raise HTTPException(404, "任务不存在")


def public_parameters(value):
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if re.search(r"(?i)(password|passwd|token|secret|api.?key|authorization)", key)
            else public_parameters(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [public_parameters(item) for item in value]
    if isinstance(value, str):
        for key, secret in os.environ.items():
            if secret and any(part in key.upper() for part in ("PASSWORD", "TOKEN", "SECRET", "API_KEY")):
                value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(https?://)[^/\s@]+@", r"\1[REDACTED]@", value)
    return value


def frozen_parameters(record, kind: str) -> dict:
    try:
        data, _, lock = policy.task_files(run_directory(record), kind)
        run = data["analysis"] if kind == "factor" else data["backtest"]
        if not isinstance(run, dict):
            raise ValueError("冻结运行参数必须是对象")
        components = {"factor": data["factor"]} if kind == "factor" else data["algos"]
        if not isinstance(components, dict):
            raise ValueError("冻结算法配置必须是对象")
        algorithms = {}
        for stage, item in components.items():
            if item is None:
                continue
            if not isinstance(item, dict) or ("params" in item and not isinstance(item["params"], dict)):
                raise ValueError("冻结组件及其参数必须是对象")
            algorithms[stage] = {
                **{key: item[key] for key in ("package", "version", "entry", "params") if key in item},
                "parameterSource": "component" if "params" in item else "shared",
            }
        analysis_fields = {"start", "end", "universe", "market_data", "symbols", "benchmark", "batch_days", "config"}
        shared = {} if kind == "factor" else {key: value for key, value in run.items() if key not in analysis_fields}
        dependencies = [{"name": item["name"], "version": item["version"]}
                        for item in lock["package"] if "virtual" not in item.get("source", {})]
        return public_parameters({
            "available": True, "error": "", "run": run,
            "dateInterval": "[start,end]" if kind == "factor" else "[start,end)",
            "algorithms": algorithms, "sharedAlgorithmParameters": shared,
            "dependencies": sorted(dependencies, key=lambda item: item["name"]), "input": data,
        })
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"available": False, "error": f"冻结参数尚未生成或无法读取：{type(error).__name__}",
                "run": {}, "dateInterval": "[start,end]" if kind == "factor" else "[start,end)",
                "algorithms": {}, "sharedAlgorithmParameters": {}, "dependencies": [], "input": {}}


CHECKPOINT_LABELS = {"factor": "因子计算", "backtest": "回测回放", "report": "报告数据"}


def checkpoint_stages(record) -> list[dict]:
    """列出研究进程已完整提交的阶段；临时目录和损坏的清单不展示。"""
    directory = run_directory(record) / "checkpoints"
    try:
        candidates = [path for path in directory.iterdir() if path.is_dir() and not path.name.startswith(".")]
    except OSError:
        return []
    stages = []
    for path in candidates:
        try:
            manifest = json.loads((path / "checkpoint.json").read_text(encoding="utf-8"))
            if manifest.get("stage") != path.name or not isinstance(manifest.get("files"), dict):
                continue
            stages.append({
                "stage": path.name, "label": CHECKPOINT_LABELS.get(path.name, path.name),
                "createdAt": manifest.get("created_at"),
                "bytes": sum(size for size in manifest["files"].values() if type(size) is int),
            })
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    order = list(CHECKPOINT_LABELS)
    return sorted(stages, key=lambda item: order.index(item["stage"]) if item["stage"] in order else len(order))


def list_item(session: Session, record, project, kind: str) -> dict:
    attempt = current_attempt(session, record, kind)
    capabilities = record_summary(session, record, kind)
    return {
        "id": str(record.id), "name": project.name if project else record.name, "kind": kind,
        "projectId": str(project.id) if project else None,
        "version": record.number if project else None,
        "note": record.note if project else "",
        "createdAt": record.created_at.isoformat(), "attemptCreatedAt": attempt.created_at.isoformat(),
        "status": attempt.status, "workflowId": attempt.workflow_id,
        **capabilities, "error": attempt.error,
        "checkpoints": [] if attempt.status == "success" else checkpoint_stages(record),
        "href": f"/projects/{project.id}?version={record.id}" if project else f"/strategies/{record.id}",
        "attempts": [summary(item) for item in session.scalars(select(TaskAttempt).where(
            TaskAttempt.run_id == record.id,
        ).order_by(TaskAttempt.number.desc()))],
    }
