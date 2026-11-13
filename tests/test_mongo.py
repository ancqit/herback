import pytest
from fastapi.testclient import TestClient

CLUSTER_URL = (
    "mongodb+srv://herback_user:p%40ssword@cluster0.abc123.mongodb.net/"
    "?retryWrites=true&w=majority&appName=herback"
)


def test_health_reports_host_without_connecting(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MONGODB_URL", CLUSTER_URL)
    monkeypatch.setenv("MONGODB_DATABASE", "herb")

    def refuse_connect(*_args, **_kwargs):
        raise AssertionError("health must not open a Mongo client")

    monkeypatch.setattr("app.database.MongoClient", refuse_connect)
    from app.database import _client
    from app.main import app

    _client.cache_clear()
    body = TestClient(app).get("/health").json()
    assert body["mongo_configured"] is True
    assert body["mongo_missing"] == []
    assert body["mongo_host"] == "cluster0.abc123.mongodb.net"
    assert "p%40ssword" not in str(body)
    assert "herback_user" not in str(body)


def test_mongo_ping_requires_a_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    from app.main import app

    response = TestClient(app).get("/mongo")
    assert response.status_code == 503
    assert response.json()["detail"] == "MONGODB_URL is not set"
    assert "mongodb" not in response.text


def test_ping_uses_the_named_database(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MONGODB_URL", CLUSTER_URL)
    monkeypatch.setenv("MONGODB_DATABASE", "herb")
    from app import database

    database._client.cache_clear()
    seen: dict = {}

    class FakeDatabase:
        def command(self, name: str):
            seen["command"] = name
            return {"ok": 1}

    class FakeClient(dict):
        def __getitem__(self, name: str):
            seen["database"] = name
            return FakeDatabase()

    monkeypatch.setattr(database, "_client", lambda *_args: FakeClient())
    from app.main import app

    body = TestClient(app).get("/mongo").json()
    assert body == {"ok": True, "database": "herb", "host": "cluster0.abc123.mongodb.net"}
    assert seen == {"database": "herb", "command": "ping"}
