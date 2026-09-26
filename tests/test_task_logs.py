"""复用 Arena 的 Worker 日志分页回归。"""
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from fastapi import HTTPException
from core.apps.projects import versions
from core.scheduler.client import DolphinSchedulerClient
from core.scheduler.task_logs import append_worker_output_lines, raw_log_lines, worker_task_log_page


class PagedLogClient:
    def __init__(self, pages: dict[int, dict[str, object]]) -> None:
        self.pages = pages
        self.requests: list[tuple[int, int]] = []

    def task_log(
        self,
        *,
        task_instance_id: int,
        skip_line_num: int,
        limit: int,
    ) -> dict[str, object]:
        assert task_instance_id == 42
        self.requests.append((skip_line_num, limit))
        return self.pages[skip_line_num]


def test_worker_log_page_uses_a_worker_scoped_cursor_across_raw_pages() -> None:
    client = PagedLogClient({
        0: {
            "message": "\n".join([
                "[INFO] 2026-08-30 10:00:00.001 +0800 - prepare task",
                "[INFO] 2026-08-30 10:00:00.002 +0800 -  -> ",
                "\tfirst",
                "\tsecond",
                "[INFO] 2026-08-30 10:00:00.003 +0800 - process running",
            ]),
            "next_line_num": 5,
            "has_more": True,
        },
        5: {
            "message": "\n".join([
                "[INFO] 2026-08-30 10:00:01.001 +0800 - -> third",
                "\tfourth",
                "[INFO] 2026-08-30 10:00:01.002 +0800 - process exited",
            ]),
            "next_line_num": 8,
            "has_more": False,
        },
    })

    first = worker_task_log_page(
        client, task_instance_id=42, skip_line_num=0, limit=2
    )
    second = worker_task_log_page(
        client,
        task_instance_id=42,
        skip_line_num=first["next_line_num"],
        limit=2,
        cursor=first["next_cursor"],
    )

    assert {key: first[key] for key in (
        "skip_line_num", "returned_lines", "next_line_num", "has_more", "message"
    )} == {
        "skip_line_num": 0,
        "returned_lines": 2,
        "next_line_num": 2,
        "has_more": True,
        "message": "first\nsecond",
    }
    assert {key: second[key] for key in (
        "skip_line_num", "returned_lines", "next_line_num", "has_more", "message"
    )} == {
        "skip_line_num": 2,
        "returned_lines": 2,
        "next_line_num": 4,
        "has_more": False,
        "message": "third\nfourth",
    }
    # The first page probes the next raw page once to make has_more exact;
    # the following request resumes from that raw page instead of rescanning 0.
    assert client.requests == [(0, 10_000), (5, 10_000), (5, 10_000)]


def test_worker_output_accepts_a_dolphinscheduler_logger_source() -> None:
    output: list[str] = []

    inside = append_worker_output_lines(
        output,
        (
            "[INFO] 2026-08-30 10:00:00 +0800 "
            "org.apache.dolphinscheduler.server.worker.runner.TaskExecuteRunnable - ->\n"
            "\tworker value"
        ),
        inside_worker_output=False,
    )

    assert output == ["worker value"]
    assert inside is True


def test_worker_output_entry_state_carries_to_the_next_raw_page() -> None:
    output: list[str] = []
    inside = append_worker_output_lines(
        output,
        "[INFO] 2026-08-30 10:00:00 +0800 - ->\n\tline one",
        inside_worker_output=False,
    )
    inside = append_worker_output_lines(
        output,
        "\tline two\n[INFO] 2026-08-30 10:00:01 +0800 - process exited",
        inside_worker_output=inside,
    )

    assert output == ["line one", "line two"]
    assert inside is False


def test_worker_log_page_preserves_a_final_blank_output_line() -> None:
    client = PagedLogClient({
        0: {
            "message": "[INFO] 2026-08-30 10:00:00 +0800 - ->\n\tvalue\n\t",
            "next_line_num": 3,
            "has_more": False,
        }
    })

    page = worker_task_log_page(
        client, task_instance_id=42, skip_line_num=0, limit=2
    )

    assert page["returned_lines"] == 2
    assert page["message"] == "value\n\n"


def test_worker_log_page_returns_an_empty_stable_cursor_before_output_exists() -> None:
    client = PagedLogClient({
        0: {
            "message": "[INFO] 2026-08-30 10:00:00 +0800 - prepare task",
            "next_line_num": 1,
            "has_more": False,
        }
    })

    page = worker_task_log_page(
        client, task_instance_id=42, skip_line_num=0, limit=100
    )

    assert page["message"] == ""
    assert page["returned_lines"] == page["next_line_num"] == 0
    assert page["has_more"] is False


@pytest.mark.parametrize("limit", [1, 7, 10000])
@pytest.mark.parametrize("use_cursor", [False, True])
def test_worker_pagination_does_not_count_scheduler_path_header(limit, use_cursor) -> None:
    worker_lines = ["first", "", *[f"line {index}" for index in range(20)], ""]
    raw_lines = [
        "[INFO] 2026-09-05 10:00:00 +0800 - preparing",
        "[INFO] 2026-09-05 10:00:01 +0800 - ->",
        *[f"\t{line}" for line in worker_lines[:9]],
        "[INFO] 2026-09-05 10:00:02 +0800 - task running",
        "[INFO] 2026-09-05 10:00:03 +0800 - ->",
        *[f"\t{line}" for line in worker_lines[9:]],
        "[INFO] 2026-09-05 10:00:04 +0800 - task ended",
    ]

    class Client:
        def task_log(self, *, skip_line_num, limit, **kwargs):
            lines = raw_lines[skip_line_num:skip_line_num + min(limit, 5)]
            end = skip_line_num + len(lines)
            prefix = "[LOG-PATH]: /logs/task.log, [HOST]: worker:1234\n" if skip_line_num == 0 else ""
            return {
                "message": prefix + "\r\n".join(lines) + ("\r\n" if lines else ""),
                "next_line_num": end,
                "has_more": end < len(raw_lines),
            }

    offset = 0
    cursor = None
    actual = []
    for _ in range(len(raw_lines) + 1):
        page = worker_task_log_page(Client(), task_instance_id=42, skip_line_num=offset, limit=limit, cursor=cursor)
        actual.extend(raw_log_lines(page["message"], page["returned_lines"]))
        offset = page["next_line_num"]
        cursor = page["next_cursor"] if use_cursor else None
        if not page["has_more"]:
            break
    else:
        pytest.fail("日志游标未结束")
    assert actual == worker_lines
    assert offset == len(worker_lines)


@pytest.fixture
def log_version(monkeypatch):
    version = SimpleNamespace(id=uuid4(), project_id=uuid4(), workflow_id=11)
    session = MagicMock()
    session.get.return_value = version
    factory = MagicMock()
    client = factory.return_value.__enter__.return_value
    client.tasks.return_value = [{"id": 42, "name": "backtest", "state": "SUCCESS"}]
    monkeypatch.setattr(versions, "DolphinSchedulerClient", factory)
    return version, session, client


def test_version_logs_pass_pagination_to_scheduler(log_version):
    version, session, client = log_version
    client.task_log.return_value = {"message": "tail", "next_line_num": 10001, "has_more": False}
    result = versions.version_logs(version.project_id, version.id, session, 42, 10000, 500)
    client.task_log.assert_called_once_with(task_instance_id=42, skip_line_num=10000, limit=500)
    assert result["message"] == "tail"
    assert result["task_instance_id"] == 42


def test_version_logs_reject_task_from_other_version(log_version):
    version, session, client = log_version
    with pytest.raises(HTTPException) as error:
        versions.version_logs(version.project_id, version.id, session, 43)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException):
        versions.download_version_log(version.project_id, version.id, 43, session)
    client.task_log.assert_not_called()
    client.download_log.assert_not_called()


def test_version_download_returns_complete_log(log_version):
    version, session, client = log_version
    message = "line\n" * 10001
    client.download_log.return_value = message
    assert versions.download_version_log(version.project_id, version.id, 42, session) == message


def test_empty_scheduler_page_keeps_offset():
    client = DolphinSchedulerClient()
    client.task_log_detail = MagicMock(return_value={"message": "", "lineNum": 1})
    result = client.task_log(task_instance_id=42, skip_line_num=10000)
    client.session.close()
    assert result["next_line_num"] == 10000
    assert result["has_more"] is False
