"""DS 3.2.2 retry commands with a transactionally verifiable acceptance result."""

import json
from contextlib import closing
from dataclasses import dataclass
from uuid import UUID

from psycopg import Error, connect
from psycopg.rows import dict_row

from config import DatabaseSettings, DolphinSchedulerSettings
from core.scheduler.client import (
    DolphinSchedulerError,
    DolphinSchedulerStateError,
    instance_input_file,
    worker_input_file,
)

RETRY_STATES = {6: 4, 3: 7, 5: 7, 16: 7}
MARKER = "solo_retry_id"


@dataclass(frozen=True)
class RetryReceipt:
    state: str
    run_times: int


def connection():
    return connect(
        host=DatabaseSettings.HOST, port=DatabaseSettings.PORT,
        user=DatabaseSettings.USERNAME, password=DatabaseSettings.PASSWORD,
        dbname=DolphinSchedulerSettings.DATABASE, connect_timeout=5,
        options="-c lock_timeout=10000 -c statement_timeout=15000", row_factory=dict_row,
    )


def locked_instance(cursor, instance_id: int, project: int, definition: int, input_file: str) -> dict:
    cursor.execute("""
        SELECT id, project_code, process_definition_code, process_definition_version,
               run_times, state, command_param, global_params, process_instance_priority, test_flag
        FROM t_ds_process_instance WHERE id = %s FOR UPDATE
    """, (instance_id,))
    row = cursor.fetchone()
    if row is None or (
        row["project_code"] != project or row["process_definition_code"] != definition
        or instance_input_file({"commandParam": row["command_param"], "globalParams": row["global_params"]})
        != worker_input_file(input_file)
    ):
        raise DolphinSchedulerError("调度实例与重试输入不一致，未执行操作")
    if type(row["run_times"]) is not int or row["run_times"] < 1:
        raise DolphinSchedulerError("调度运行次数无效，未执行重试")
    return row


def receipt(cursor, row: dict, retry_id: UUID | None, expected_run_times: int) -> RetryReceipt:
    # Separate statements after FOR UPDATE see a retry commit/consumption that finished while waiting.
    parameters = json.loads(row["command_param"] or "{}")
    if row["run_times"] > expected_run_times:
        return RetryReceipt("started", row["run_times"])
    if retry_id is not None and parameters.get(MARKER) == str(retry_id):
        return RetryReceipt("started", row["run_times"])
    if row["run_times"] != expected_run_times:
        raise DolphinSchedulerStateError("调度运行次数已变化，请刷新任务")
    for table, state in (("t_ds_command", "pending"), ("t_ds_error_command", "rejected")):
        cursor.execute(f"""
            SELECT command_param FROM {table}
            WHERE process_instance_id = %s AND process_definition_code = %s
              AND command_type IN (4, 7)
        """, (row["id"], row["process_definition_code"]))
        for item in cursor.fetchall():
            parameters = json.loads(item["command_param"] or "{}")
            if retry_id is not None and parameters.get(MARKER) == str(retry_id):
                return RetryReceipt(state, row["run_times"])
            if state == "pending":
                if retry_id is None:
                    return RetryReceipt("pending", row["run_times"])
                raise DolphinSchedulerStateError("调度器已有待处理重试命令，未重复提交")
    return RetryReceipt("legacy_absent" if retry_id is None else "absent", row["run_times"])


def inspect_retry(instance_id: int, project: int, definition: int, input_file: str,
                  expected_run_times: int, retry_id: UUID | None) -> RetryReceipt:
    try:
        with connection() as database, database.cursor() as cursor:
            row = locked_instance(cursor, instance_id, project, definition, input_file)
            return receipt(cursor, row, retry_id, expected_run_times)
    except (Error, ValueError, TypeError, KeyError) as error:
        raise DolphinSchedulerError("无法核对重试命令，保留待确认状态") from error


def submit_retry(instance_id: int, project: int, definition: int, input_file: str,
                 expected_run_times: int, retry_id: UUID) -> RetryReceipt:
    committing = False
    result = None
    try:
        with closing(connection()) as database, database.cursor() as cursor:
            row = locked_instance(cursor, instance_id, project, definition, input_file)
            existing = receipt(cursor, row, retry_id, expected_run_times)
            if existing.state != "absent":
                database.rollback()
                return existing
            command_type = RETRY_STATES.get(row["state"])
            if command_type is None:
                raise DolphinSchedulerStateError("只能重试失败或已终止的任务")
            cursor.execute("SELECT id FROM t_ds_user WHERE user_name = %s", (DolphinSchedulerSettings.USERNAME,))
            user = cursor.fetchone()
            if user is None:
                raise DolphinSchedulerError("调度用户不存在，未提交重试")
            parameters = json.loads(row["command_param"] or "{}")
            parameters[MARKER] = str(retry_id)
            parameters.setdefault("StartParams", json.dumps({"input_file": worker_input_file(input_file)}))
            parameters.setdefault("schedule_timezone", DolphinSchedulerSettings.TIME_ZONE)
            if command_type == 7:
                parameters["ProcessInstanceId"] = str(instance_id)
            # Match FailureRecovery/RepeatRunning's 3.2.2 Command builder defaults.
            cursor.execute("""
                INSERT INTO t_ds_command (
                    command_type, command_param, process_definition_code, process_definition_version,
                    process_instance_id, executor_id, test_flag, task_depend_type, failure_strategy,
                    process_instance_priority, update_time
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, 2, 1, %s, localtimestamp)
            """, (
                command_type, json.dumps(parameters), definition, row["process_definition_version"],
                instance_id, user["id"] if command_type == 4 else 0, row["test_flag"],
                2 if command_type == 4 else row["process_instance_priority"],
            ))
            # Only a lost COMMIT response is ambiguous. Both submission and inspection lock the same row.
            committing = True
            database.commit()
            result = RetryReceipt("pending", row["run_times"])
        return result
    except (Error, ValueError, TypeError, KeyError) as error:
        if result is not None:
            return result
        raise DolphinSchedulerError(
            "重试命令提交结果待核对" if committing else "重试命令未提交",
            submission_unknown=committing,
        ) from error
