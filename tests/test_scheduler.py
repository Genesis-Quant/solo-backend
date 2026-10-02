from unittest.mock import Mock

import pytest
import requests
from core.scheduler.client import (
    DolphinSchedulerClient,
    DolphinSchedulerError,
    validate_input_file,
)


@pytest.mark.parametrize(
    "path",
    [
        "/tmp/input.json",
        "/shared/runs/../input.json",
        "/shared/runs/$(id).json",
        '/shared/runs/a".json',
        "/shared/runs//input.json",
        "/shared/runs/input.txt",
    ],
)
def test_reject_unsafe_input(path):
    with pytest.raises(ValueError):
        validate_input_file(path)


def test_reject_backtest_workflow_alias():
    client = DolphinSchedulerClient()
    client.request = Mock()
    with pytest.raises(ValueError, match="未知工作流"):
        client.definition(1, "backtest")
    client.request.assert_not_called()


def test_submit_does_not_retry_post_and_uses_root_tenant():
    client = DolphinSchedulerClient()
    client.request = Mock(return_value={"accepted": True})
    client.project_code = Mock(return_value=10)
    client.definition = Mock(return_value={"code": 20})
    result = client.start("factor", "/shared/runs/run-1/input.json")
    assert result == {"accepted": True}
    args = client.request.call_args.args
    assert args[0:2] == ("POST", "/projects/10/executors/start-process-instance")
    assert args[2]["tenantCode"] == "root"
    assert args[2]["processDefinitionCode"] == 20
    retry = client.session.get_adapter("http://").max_retries
    assert retry.allowed_methods == {"GET"}
    client.session.close()


def test_log_empty_page_keeps_offset():
    client = DolphinSchedulerClient()
    client.request = Mock(return_value={"message": "", "lineNum": 1})
    assert client.log(1, offset=20) == {"message": "", "next_offset": 20}
    client.session.close()


def test_api_failure_is_not_success():
    client = DolphinSchedulerClient()
    client.session.request = Mock(
        return_value=Mock(
            json=Mock(return_value={"code": 100, "success": False, "msg": "rejected"}),
        )
    )
    with pytest.raises(DolphinSchedulerError, match="rejected"):
        client.request("POST", "/test")
    client.session.close()


def test_transport_error_does_not_include_secret():
    client = DolphinSchedulerClient()
    client.session.request = Mock(
        side_effect=requests.ConnectionError("password=secret")
    )
    with pytest.raises(DolphinSchedulerError) as error:
        client.request("POST", "/login", {"userPassword": "secret"})
    assert "secret" not in str(error.value)
    client.session.close()


@pytest.mark.parametrize("path,unknown", [
    ("/login", False),
    ("/projects/10/executors/start-process-instance", True),
])
def test_transport_failure_identifies_uncertain_submission(path, unknown):
    client = DolphinSchedulerClient()
    client.session.request = Mock(side_effect=requests.Timeout())
    with pytest.raises(DolphinSchedulerError) as error:
        client.request("POST", path)
    assert error.value.submission_unknown is unknown
    client.session.close()


@pytest.mark.parametrize("status,unknown", [(403, False), (500, True)])
def test_submission_http_failure_classification(status, unknown):
    response = requests.Response()
    response.status_code = status
    client = DolphinSchedulerClient()
    client.session.request = Mock(return_value=response)
    with pytest.raises(DolphinSchedulerError) as error:
        client.request("POST", "/projects/10/executors/start-process-instance")
    assert error.value.submission_unknown is unknown
    client.session.close()


def test_explicit_submission_rejection_is_retryable():
    client = DolphinSchedulerClient()
    client.session.request = Mock(return_value=Mock(
        json=Mock(return_value={"code": 100, "success": False, "msg": "rejected"}),
    ))
    with pytest.raises(DolphinSchedulerError) as error:
        client.request("POST", "/projects/10/executors/start-process-instance")
    assert error.value.submission_unknown is False
    client.session.close()


def test_log_failure_is_reported_and_next_request_uses_api_again():
    client = DolphinSchedulerClient()
    error = DolphinSchedulerError("Worker 地址不可达")
    client.request = Mock(side_effect=[error, {"message": "", "lineNum": 1}])
    try:
        with pytest.raises(DolphinSchedulerError) as caught:
            client.task_log(task_instance_id=42, skip_line_num=20, limit=5)
        assert caught.value is error
        assert client.log(42, offset=20, limit=5) == {"message": "", "next_offset": 20}
        assert client.request.call_count == 2
        client.request.assert_called_with("GET", "/log/detail", params={
            "taskInstanceId": 42, "skipLineNum": 20, "limit": 5,
        })
    finally:
        client.session.close()


@pytest.mark.parametrize("body,content_type,status,expected", [
    ("日志\n\n".encode(), "application/octet-stream", 200, "日志\n\n"),
    (b"log\xff\n", "text/plain", 200, "log\ufffd\n"),
    (b'{"code":100,"msg":"worker unavailable"}', "application/json", 200, None),
    (b'{"detail":"worker unavailable"}', "application/problem+json", 200, None),
    (b"forbidden", "text/html", 403, None),
    (b"upstream error", "text/plain", 502, None),
])
def test_download_uses_api_response_or_reports_failure(body, content_type, status, expected):
    response = requests.Response()
    response.status_code = status
    response.headers["Content-Type"] = content_type
    response._content = body
    response._content_consumed = True
    client = DolphinSchedulerClient()
    client.project_code = Mock(return_value=10)
    client.session.get = Mock(return_value=response)
    try:
        if expected is None:
            with pytest.raises(DolphinSchedulerError, match="日志下载失败"):
                client.download_log(42)
        else:
            assert client.download_log(42) == expected
        assert client.session.get.call_args.args[0].endswith("/log/10/download-log")
        assert client.session.get.call_args.kwargs == {
            "params": {"taskInstanceId": 42}, "timeout": client.timeout,
        }
    finally:
        client.session.close()


@pytest.mark.parametrize("error", [requests.Timeout("secret"), requests.ConnectionError("secret")])
def test_log_download_transport_error_is_not_file_fallback(error):
    client = DolphinSchedulerClient()
    client.project_code = Mock(return_value=10)
    client.session.get = Mock(side_effect=error)
    try:
        with pytest.raises(DolphinSchedulerError) as caught:
            client.download_log(42)
        assert str(caught.value) == "DolphinScheduler 日志下载失败"
    finally:
        client.session.close()
