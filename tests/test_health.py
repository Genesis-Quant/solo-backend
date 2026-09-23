from fastapi.testclient import TestClient

from core.apps.system import views
from core.database.health import DatabaseError, DatabaseInfo
from main import app


def test_health(monkeypatch) -> None:
    monkeypatch.setattr(views, "check_database", lambda: DatabaseInfo("solo", "public"))
    with TestClient(app) as client:
        for path in ("/health", "/api/v1/health"):
            response = client.get(path)
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "database": "solo", "schema": "public"}


def test_database_unavailable(monkeypatch) -> None:
    def unavailable():
        raise DatabaseError("PostgreSQL 连接失败")
    monkeypatch.setattr(views, "check_database", unavailable)
    with TestClient(app) as client:
        response = client.get("/api/v1/health")
        assert response.status_code == 503
        assert response.json() == {"detail": "PostgreSQL 连接失败"}
