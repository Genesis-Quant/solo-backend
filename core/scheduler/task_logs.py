"""Arena 的 Worker 输出解析与分页游标。"""
from base64 import urlsafe_b64decode, urlsafe_b64encode
from binascii import Error as BinasciiError
from dataclasses import dataclass
import json
import re
from typing import Any

from core.scheduler.client import DolphinSchedulerClient


_DOLPHINSCHEDULER_ENTRY = re.compile(
    r"^\[([A-Z]+)\]\s+\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"
    r"(?:[.,]\d{1,6})?(?:\s+[+-]\d{4})?\s+(.*)$"
)
_WORKER_SCAN_PAGE_SIZE = 10_000


@dataclass(frozen=True)
class WorkerLogCursor:
    task_instance_id: int
    visible_line_num: int
    raw_line_num: int
    inside_worker_output: bool


def worker_task_log_page(
    client: DolphinSchedulerClient,
    *,
    task_instance_id: int,
    skip_line_num: int,
    limit: int,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Return a page whose cursor counts only Worker stdout/stderr lines.

    DolphinScheduler stores child-process output as entries whose message is
    ``->`` followed by continuation lines.  The scheduler API cannot filter
    those entries, so an opaque cursor carries the raw offset and entry state
    between calls.  Calls without that cursor scan from the raw-log start to
    honor ``skip_line_num``.  Reading one extra Worker line makes ``has_more``
    exact without making the raw offset part of the public contract.
    """
    state = decode_worker_log_cursor(
        cursor,
        task_instance_id=task_instance_id,
        skip_line_num=skip_line_num,
    ) if cursor else WorkerLogCursor(task_instance_id, 0, 0, False)
    raw_cursor = state.raw_line_num
    visible_line_num = state.visible_line_num
    inside_worker_output = state.inside_worker_output
    selected: list[str] = []
    next_state = state
    has_more = False

    while True:
        page = client.task_log(
            task_instance_id=task_instance_id,
            skip_line_num=raw_cursor,
            limit=_WORKER_SCAN_PAGE_SIZE,
        )
        next_raw_cursor = int(page.get("next_line_num") or raw_cursor)
        # /log/detail prepends a display-only header on its first page. It is
        # not included in lineNum/skipLineNum and must not shift raw cursors.
        message = str(page.get("message") or "")
        if raw_cursor == 0 and message.startswith("[LOG-PATH]:"):
            message = message.partition("\n")[2]
        raw_lines = raw_log_lines(message, max(0, next_raw_cursor - raw_cursor))
        for offset, line in enumerate(raw_lines):
            before_line = WorkerLogCursor(
                task_instance_id,
                visible_line_num,
                raw_cursor + offset,
                inside_worker_output,
            )
            inside_worker_output, worker_line = parse_worker_output_line(
                line,
                inside_worker_output=inside_worker_output,
            )
            if worker_line is None:
                continue
            if visible_line_num < skip_line_num:
                visible_line_num += 1
                continue
            if len(selected) >= limit:
                next_state = before_line
                has_more = True
                break
            selected.append(worker_line)
            visible_line_num += 1
        if has_more:
            break
        next_state = WorkerLogCursor(
            task_instance_id,
            visible_line_num,
            next_raw_cursor,
            inside_worker_output,
        )
        if not page.get("has_more") or next_raw_cursor <= raw_cursor:
            break
        raw_cursor = next_raw_cursor

    next_line_num = skip_line_num + len(selected)
    return {
        "skip_line_num": skip_line_num,
        "returned_lines": len(selected),
        "next_line_num": next_line_num,
        "has_more": has_more,
        # A second trailing newline is required to encode a real final blank
        # line; consumers remove only the transport delimiter.
        "message": "\n".join(selected) + ("\n" if selected and selected[-1] == "" else ""),
        "next_cursor": encode_worker_log_cursor(next_state),
    }


def append_worker_output_lines(
    output: list[str],
    message: str,
    *,
    inside_worker_output: bool,
) -> bool:
    """Append Worker child-process lines from one raw DolphinScheduler page."""
    lines = raw_log_lines(message)
    for line in lines:
        inside_worker_output, worker_line = parse_worker_output_line(
            line,
            inside_worker_output=inside_worker_output,
        )
        if worker_line is not None:
            output.append(worker_line)
    return inside_worker_output


def parse_worker_output_line(
    line: str,
    *,
    inside_worker_output: bool,
) -> tuple[bool, str | None]:
    entry = _DOLPHINSCHEDULER_ENTRY.match(line)
    if entry:
        entry_message = dolphin_scheduler_entry_message(entry.group(2))
        inside_worker_output = (
            entry_message == "->" or entry_message.startswith("-> ")
        )
        if inside_worker_output:
            inline_output = entry_message[2:].lstrip()
            return inside_worker_output, inline_output or None
        return inside_worker_output, None
    if inside_worker_output:
        return inside_worker_output, line[1:] if line.startswith("\t") else line
    return inside_worker_output, None


def dolphin_scheduler_entry_message(remainder: str) -> str:
    value = remainder.strip()
    if value.startswith("-"):
        return value[1:].lstrip()
    separator = value.find(" - ")
    return value[separator + 3:].lstrip() if separator >= 0 else value


def raw_log_lines(message: str, expected_count: int | None = None) -> list[str]:
    lines = message.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if expected_count is None:
        if lines and lines[-1] == "":
            lines.pop()
        return lines
    while len(lines) > expected_count and lines[-1] == "":
        lines.pop()
    return lines


def encode_worker_log_cursor(cursor: WorkerLogCursor) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "task": cursor.task_instance_id,
            "visible": cursor.visible_line_num,
            "raw": cursor.raw_line_num,
            "inside": cursor.inside_worker_output,
        },
        separators=(",", ":"),
    ).encode()
    return urlsafe_b64encode(payload).decode().rstrip("=")


def decode_worker_log_cursor(
    value: str,
    *,
    task_instance_id: int,
    skip_line_num: int,
) -> WorkerLogCursor:
    try:
        payload = json.loads(
            urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode()
        )
        cursor = WorkerLogCursor(
            task_instance_id=int(payload["task"]),
            visible_line_num=int(payload["visible"]),
            raw_line_num=int(payload["raw"]),
            inside_worker_output=payload["inside"],
        )
    except (BinasciiError, KeyError, TypeError, ValueError, UnicodeError) as error:
        raise ValueError("Worker 日志 cursor 无效") from error
    if (
        payload.get("v") != 1
        or not isinstance(cursor.inside_worker_output, bool)
        or cursor.task_instance_id != task_instance_id
        or cursor.visible_line_num != skip_line_num
        or cursor.raw_line_num < 0
    ):
        raise ValueError("Worker 日志 cursor 与当前 Task 或行号不匹配")
    return cursor
