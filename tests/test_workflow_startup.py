import socket
import sys
from threading import Event
from time import monotonic
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest
from config import DolphinSchedulerSettings as Settings
from core.scheduler import workflows


@pytest.fixture
def clock(monkeypatch):
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(workflows, "monotonic", lambda: clock.now)

    class ProbeEvent:
        def __init__(self):
            self.stopped = False

        def is_set(self):
            return self.stopped

        def set(self):
            self.stopped = True

        def wait(self, seconds):
            clock.now += seconds
            return self.stopped

    class InlineThread:
        def __init__(self, *, target, name, daemon):
            assert name == "gateway-readiness" and daemon
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(workflows, "Event", ProbeEvent)
    monkeypatch.setattr(workflows, "Thread", InlineThread)
    return clock


def test_gateway_ready_closes_probe_connection(monkeypatch, clock):
    connection = MagicMock()
    connect = Mock(return_value=connection)
    monkeypatch.setattr(workflows.socket, "create_connection", connect)
    workflows.wait_for_gateway()
    connect.assert_called_once_with((Settings.HOST, Settings.GATEWAY_PORT), timeout=2)
    connection.__exit__.assert_called_once()
    assert clock.now == 0


@pytest.mark.parametrize("error", [ConnectionRefusedError, TimeoutError, socket.gaierror])
def test_gateway_waits_for_readiness_without_submitting(monkeypatch, clock, error):
    connection = MagicMock()
    connect = Mock(side_effect=[error("not ready"), error("not ready"), connection])
    monkeypatch.setattr(workflows.socket, "create_connection", connect)
    workflows.wait_for_gateway(timeout=5)
    assert connect.call_count == 3
    assert clock.now == 2
    connection.__exit__.assert_called_once()


def test_gateway_wait_has_a_deadline(monkeypatch, clock):
    connect = Mock(side_effect=ConnectionRefusedError("not ready"))
    monkeypatch.setattr(workflows.socket, "create_connection", connect)
    with pytest.raises(RuntimeError, match="Gateway 在 2.5 秒内未就绪"):
        workflows.wait_for_gateway(timeout=2.5)
    assert clock.now == 2.5
    assert connect.call_count == 3
    assert connect.call_args.kwargs["timeout"] == 0.5


def test_connection_timeout_does_not_extend_readiness_deadline(monkeypatch, clock):
    def connect(address, timeout):
        clock.now += timeout
        raise TimeoutError("not ready")

    monkeypatch.setattr(workflows.socket, "create_connection", connect)
    with pytest.raises(RuntimeError, match="未就绪"):
        workflows.wait_for_gateway(timeout=1.5)
    assert clock.now == 1.5


@pytest.mark.parametrize("timeout", [0, -1])
def test_nonpositive_deadline_does_not_start_a_probe(monkeypatch, timeout):
    connect = Mock()
    thread = Mock()
    monkeypatch.setattr(workflows.socket, "create_connection", connect)
    monkeypatch.setattr(workflows, "Thread", thread)
    with pytest.raises(RuntimeError, match="未就绪"):
        workflows.wait_for_gateway(timeout=timeout)
    connect.assert_not_called()
    thread.assert_not_called()


def test_success_after_dns_deadline_is_rejected(monkeypatch, clock):
    def resolve(*args):
        clock.now += 61
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 25333))]

    connection = MagicMock()
    monkeypatch.setattr(workflows.socket, "getaddrinfo", resolve)
    monkeypatch.setattr(workflows.socket, "socket", Mock(return_value=connection))
    with pytest.raises(RuntimeError, match="60 秒内未就绪"):
        workflows.wait_for_gateway(timeout=60)
    connection.connect.assert_called_once_with(("127.0.0.1", 25333))
    connection.__exit__.assert_called_once()
    assert clock.now == 61


def test_multiple_address_timeouts_share_caller_deadline(monkeypatch):
    entered = Event()
    release = Event()
    all_closed = Event()
    total_timeout = 0.1
    sockets = []

    def connect(address):
        entered.set()
        assert release.wait(5)
        raise TimeoutError("not ready")

    def make_socket(*args):
        connection = Mock()
        connection.connect.side_effect = connect
        sockets.append(connection)
        if len(sockets) == 2:
            connection.close.side_effect = all_closed.set
        return connection

    addresses = [
        (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 25333)),
        (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 25333)),
    ]
    monkeypatch.setattr(workflows.socket, "getaddrinfo", Mock(return_value=addresses))
    monkeypatch.setattr(workflows.socket, "socket", make_socket)
    started = monotonic()
    try:
        with pytest.raises(RuntimeError, match="未就绪"):
            workflows.wait_for_gateway(timeout=total_timeout)
        elapsed = monotonic() - started
        assert entered.is_set()
        assert 0.07 <= elapsed < 1
        assert not release.is_set()
    finally:
        release.set()
        assert all_closed.wait(2)
    assert len(sockets) == 2
    assert all(connection.close.call_count == 1 for connection in sockets)


def test_slow_dns_does_not_block_caller_and_late_socket_is_closed(monkeypatch):
    entered = Event()
    release = Event()
    closed = Event()
    stopped = Event()
    monkeypatch.setattr(workflows, "Event", lambda: stopped)

    def resolve(*args):
        entered.set()
        assert release.wait(5)
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 25333))]

    connection = MagicMock()
    connection.__exit__.side_effect = lambda *args: closed.set()
    resolver = Mock(side_effect=resolve)
    monkeypatch.setattr(workflows.socket, "getaddrinfo", resolver)
    monkeypatch.setattr(workflows.socket, "socket", Mock(return_value=connection))
    started = monotonic()
    try:
        with pytest.raises(RuntimeError, match="未就绪"):
            workflows.wait_for_gateway(timeout=0.1)
        elapsed = monotonic() - started
        assert entered.is_set()
        assert 0.07 <= elapsed < 1
        assert stopped.is_set()
        assert not closed.is_set()
    finally:
        release.set()
        assert closed.wait(2)
    resolver.assert_called_once()
    connection.__exit__.assert_called_once()


def test_late_failed_probe_is_not_retried(monkeypatch):
    entered = Event()
    release = Event()
    finished = Event()
    stopped = Event()
    monkeypatch.setattr(workflows, "Event", lambda: stopped)

    def connect(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        finished.set()
        raise ConnectionRefusedError("not ready")

    connector = Mock(side_effect=connect)
    monkeypatch.setattr(workflows.socket, "create_connection", connector)
    try:
        with pytest.raises(RuntimeError, match="未就绪"):
            workflows.wait_for_gateway(timeout=0.1)
        assert entered.is_set() and stopped.is_set()
    finally:
        release.set()
        assert finished.wait(2)
    connector.assert_called_once()


def test_unexpected_probe_error_is_not_hidden(monkeypatch, clock):
    monkeypatch.setattr(workflows.socket, "create_connection", Mock(side_effect=ValueError("invalid")))
    with pytest.raises(ValueError, match="invalid"):
        workflows.wait_for_gateway()


def test_sync_stops_before_sdk_import_when_gateway_unavailable(monkeypatch):
    configure = Mock()
    wait = Mock(side_effect=RuntimeError("gateway unavailable"))
    monkeypatch.setattr(Settings, "configure_sdk_environment", configure)
    monkeypatch.setattr(workflows, "wait_for_gateway", wait)
    with pytest.raises(RuntimeError, match="gateway unavailable"):
        workflows.sync_workflows()
    configure.assert_called_once_with()
    wait.assert_called_once_with()


def test_sync_creates_entities_only_once_after_readiness(monkeypatch):
    events = []
    monkeypatch.setattr(Settings, "configure_sdk_environment", lambda: events.append("configure"))
    monkeypatch.setattr(workflows, "wait_for_gateway", lambda: events.append("ready"))

    user = Mock()
    user.return_value.create_if_not_exists.side_effect = lambda: events.append("user")
    project = Mock()
    project.return_value.create_if_not_exists.side_effect = lambda _: events.append("project")
    workflow = MagicMock()
    workflow.return_value.__enter__.return_value = workflow.return_value
    workflow.return_value.submit.side_effect = range(1, 7)
    shell = Mock()
    for name, exports in {
        "pydolphinscheduler.models.user": {"User": user},
        "pydolphinscheduler.models.project": {"Project": project},
        "pydolphinscheduler.core.workflow": {"Workflow": workflow},
        "pydolphinscheduler.tasks.shell": {"Shell": shell},
    }.items():
        monkeypatch.setitem(sys.modules, name, SimpleNamespace(**exports))

    result = workflows.sync_workflows()
    assert events == ["configure", "ready", "user", "project"]
    user.assert_called_once()
    user.return_value.create_if_not_exists.assert_called_once_with()
    project.assert_called_once()
    assert list(result) == ["factor", "model", "optimize", "control", "execution", "strategy"]
    assert list(result.values()) == list(range(1, 7))
    assert workflow.call_count == shell.call_count == workflow.return_value.submit.call_count == 6
