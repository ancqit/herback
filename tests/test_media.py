import os

import pytest
from fastapi.testclient import TestClient

ENV_NAMES = (
    "R2_ACCOUNT_ID",
    "R2_ACCESS_KEY_ID",
    "R2_SECRET_ACCESS_KEY",
    "R2_BUCKET",
    "R2_PUBLIC_BASE_URL",
    "R2_ENDPOINT",
    "R2_SIGN_EXPIRES_SECONDS",
    "HERB_API_KEY",
    "CORS_ORIGINS",
    "MONGODB_URL",
    "MONGODB_DATABASE",
)


@pytest.fixture(autouse=True)
def clear_storage_env(monkeypatch: pytest.MonkeyPatch):
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    from app import r2

    r2._client.cache_clear()
    from app import database

    database._client.cache_clear()
    yield
    r2._client.cache_clear()
    database._client.cache_clear()


def _configure(monkeypatch: pytest.MonkeyPatch, **extra: str) -> None:
    monkeypatch.setenv("R2_ACCOUNT_ID", "abc123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "herb-media")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setenv("HERB_API_KEY", "test-key")
    for name, value in extra.items():
        monkeypatch.setenv(name, value)


def test_health_lists_missing_names():
    from app.main import app

    body = TestClient(app).get("/health").json()
    assert body["status"] == "ok"
    assert body["r2_configured"] is False
    assert "R2_ACCESS_KEY_ID" in body["r2_missing"]
    assert "R2_SECRET_ACCESS_KEY" in body["r2_missing"]
    assert body["api_key_configured"] is False
    assert body["mongo_configured"] is False
    assert body["mongo_missing"] == ["MONGODB_URL"]
    assert body["mongo_host"] == ""
    assert all(isinstance(name, str) and name.isupper() for name in body["r2_missing"])


def test_health_when_configured(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.main import app

    body = TestClient(app).get("/health").json()
    assert body["r2_configured"] is True
    assert body["r2_missing"] == []
    assert body["api_key_configured"] is True


def test_presign_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    monkeypatch.delenv("HERB_API_KEY")
    from app.main import app

    response = TestClient(app).post("/media/sign", json={"kind": "image", "content_type": "image/jpeg"})
    assert response.status_code == 503


def test_presign_rejects_bad_api_key(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.main import app

    response = TestClient(app).post(
        "/media/sign",
        json={"kind": "image", "content_type": "image/jpeg"},
        headers={"X-Herb-Key": "nope"},
    )
    assert response.status_code == 401


def test_presign_put_is_local(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.main import app

    response = TestClient(app).post(
        "/media/sign",
        json={"kind": "image", "content_type": "image/jpg", "filename": "shelf.jpeg"},
        headers={"Authorization": "Bearer test-key"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["object_key"].startswith("herb/images/")
    assert body["object_key"].endswith(".jpg")
    assert body["public_url"] == f"https://media.example/{body['object_key']}"
    assert body["upload_method"] == "PUT"
    assert body["content_type"] == "image/jpeg"
    assert body["headers"] == {"Content-Type": "image/jpeg"}
    assert "X-Amz-Signature=" in body["upload_url"]
    assert "herb-media" in body["upload_url"] or "herb-media" in body["upload_url"].lower()
    assert body["max_bytes"] == 8 * 1024 * 1024


def test_video_sign_matches_junction_shorts(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    monkeypatch.delenv("HERB_API_KEY")
    from app.main import app

    response = TestClient(app).post(
        "/api/media/sign",
        json={
            "kind": "video",
            "place": "schedule",
            "content_type": "application/mp4",
            "filename": "circle.m4v",
        },
        headers={"Authorization": "Bearer 1001"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "video"
    assert body["object_key"].startswith("herb/videos/schedule/")
    assert body["object_key"].endswith(".mp4")
    assert body["content_type"] == "video/mp4"
    assert body["upload_method"] == "PUT"
    assert body["headers"] == {"Content-Type": "video/mp4"}
    assert body["max_bytes"] == 40 * 1024 * 1024
    assert "shorts/" not in body["object_key"]


def test_video_aliases_and_places(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.main import app
    client = TestClient(app)
    headers = {"X-Herb-Key": "test-key"}

    webm = client.post(
        "/media/sign",
        json={"kind": "video", "place": "coach", "content_type": "video/x-matroska", "filename": "talk.mkv"},
        headers=headers,
    )
    assert webm.status_code == 200
    assert webm.json()["object_key"].startswith("herb/videos/coach/")
    assert webm.json()["object_key"].endswith(".webm")
    assert webm.json()["content_type"] == "video/webm"

    mov = client.post(
        "/media/sign",
        json={"kind": "video", "place": "centre", "content_type": "", "filename": "room.mov"},
        headers=headers,
    )
    assert mov.status_code == 200
    assert mov.json()["object_key"].startswith("herb/videos/centre/")
    assert mov.json()["content_type"] == "video/quicktime"

    picture = client.post(
        "/media/sign",
        json={"kind": "image", "place": "centre", "content_type": "image/png", "filename": "door.png"},
        headers=headers,
    )
    assert picture.status_code == 200
    assert picture.json()["object_key"].startswith("herb/images/centre/")


def test_user_pass_cannot_sign(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.main import app

    response = TestClient(app).post(
        "/media/sign",
        json={"kind": "video", "place": "coach", "content_type": "video/mp4"},
        headers={"Authorization": "Bearer 2001"},
    )
    assert response.status_code == 401


def test_presign_rejects_unknown_type(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.main import app

    response = TestClient(app).post(
        "/media/sign",
        json={"kind": "document", "content_type": "text/plain", "filename": "notes.txt"},
        headers={"X-Herb-Key": "test-key"},
    )
    assert response.status_code == 400


def test_delete_rejects_foreign_key(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.main import app

    response = TestClient(app).request(
        "DELETE",
        "/media",
        json={"object_key": "shorts/not-ours.mp4"},
        headers={"X-Herb-Key": "test-key"},
    )
    assert response.status_code == 400


def test_server_upload_puts_bytes(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app import r2
    from app.main import app

    stored: dict = {}

    class FakeClient:
        def put_object(self, **kwargs):
            stored.update(kwargs)

        def generate_presigned_url(self, *args, **kwargs):
            raise AssertionError("presign should not run")

    monkeypatch.setattr(r2, "client_for", lambda settings: FakeClient())
    response = TestClient(app).post(
        "/media",
        data={"kind": "document"},
        files={"file": ("plan.pdf", b"%PDF-1.4", "application/pdf")},
        headers={"X-Herb-Key": "test-key"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["object_key"].startswith("herb/documents/")
    assert body["public_url"].endswith(body["object_key"])
    assert stored["Bucket"] == "herb-media"
    assert stored["Key"] == body["object_key"]
    assert stored["Body"] == b"%PDF-1.4"
    assert stored["ContentType"] == "application/pdf"
    assert stored["CacheControl"].startswith("public")


def test_cors_keeps_herbant_and_merges_extra(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://preview.example")
    from app.cors_config import load_cors_origins

    origins = load_cors_origins()
    assert "https://herbant.vercel.app" in origins
    assert "http://localhost:4200" in origins
    assert "https://preview.example" in origins


def test_endpoint_falls_back_to_account_host(monkeypatch: pytest.MonkeyPatch):
    _configure(monkeypatch)
    from app.r2 import load_settings

    assert load_settings().endpoint == "https://abc123.r2.cloudflarestorage.com"
    monkeypatch.setenv("R2_ENDPOINT", "https://abc123.eu.r2.cloudflarestorage.com")
    assert load_settings().endpoint == "https://abc123.eu.r2.cloudflarestorage.com"
