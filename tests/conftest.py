import pytest

from config import DatabaseSettings


@pytest.fixture(autouse=True)
def database_configuration(monkeypatch) -> None:
    """健康检查单测使用模拟连接，不依赖本机的数据库凭据。"""
    monkeypatch.setattr(DatabaseSettings, "PASSWORD", "test-password")
