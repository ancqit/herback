import pytest
from fastapi.testclient import TestClient

CLUSTER_URL = (
    "mongodb+srv://herback_user:p%40ssword@cluster0.abc123.mongodb.net/"
    "?retryWrites=true&w=majority&appName=herback"
)


def test_health_pings_mongo_and_hides_the_url(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MONGODB_URL", CLUSTER_URL)
    monkeypatch.setenv("MONGODB_DATABASE", "herb")
    seen: dict = {}

    class FakeDatabase:
        def command(self, name: str):
            seen["command"] = name
            return {"ok": 1}

    class FakeClient:
        def __init__(self, url: str, **kwargs):
            seen["url"] = url
            seen["timeout"] = kwargs.get("serverSelectionTimeoutMS")

        def __getitem__(self, name: str):
            seen["database"] = name
            return FakeDatabase()

        def close(self) -> None:
            seen["closed"] = True

    monkeypatch.setattr("app.database.MongoClient", FakeClient)
    from app.main import app

    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["mongo_configured"] is True
    assert body["mongo_missing"] == []
    assert body["mongo_host"] == "cluster0.abc123.mongodb.net"
    assert body["mongo_ok"] is True
    assert seen["command"] == "ping"
    assert seen["database"] == "herb"
    assert seen["timeout"] == 2_000
    assert seen["closed"] is True
    assert "p%40ssword" not in str(body)
    assert "herback_user" not in str(body)


def test_health_stays_up_when_the_ping_misses(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MONGODB_URL", CLUSTER_URL)
    from pymongo.errors import ServerSelectionTimeoutError

    class QuietClient:
        def __init__(self, *_args, **_kwargs):
            return None

        def __getitem__(self, _name: str):
            return self

        def command(self, _name: str):
            raise ServerSelectionTimeoutError("timed out")

        def close(self) -> None:
            return None

    monkeypatch.setattr("app.database.MongoClient", QuietClient)
    from app.main import app

    response = TestClient(app).get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["mongo_ok"] is False
    assert body["mongo_host"] == "cluster0.abc123.mongodb.net"
    assert "p%40ssword" not in response.text
    assert "herback_user" not in response.text


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
