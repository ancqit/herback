"""Scrap portal stores cards in herback's R2 bucket. No network."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.signed: list[str] = []

    def read_object(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def write_object(self, key: str, body: bytes, **_kwargs) -> None:
        self.objects[key] = body

    def delete_key(self, key: str) -> None:
        self.objects.pop(key, None)

    def presign_put_key(self, key: str, content_type: str) -> str:
        self.signed.append(f"{key}:{content_type}")
        return f"https://example.r2.cloudflarestorage.com/{key}"

    def public_url(self, key: str) -> str:
        return f"https://media.example/{key}"

    def require_r2(self) -> None:
        return None


def _install(monkeypatch: pytest.MonkeyPatch, token: str = "desk-token") -> Bucket:
    bucket = Bucket()
    monkeypatch.setenv("UPLOAD_TOKEN", token)
    monkeypatch.setattr("app.scrap_portal.r2.read_object", bucket.read_object)
    monkeypatch.setattr("app.scrap_portal.r2.write_object", bucket.write_object)
    monkeypatch.setattr("app.scrap_portal.r2.delete_key", bucket.delete_key)
    monkeypatch.setattr("app.scrap_portal.r2.presign_put_key", bucket.presign_put_key)
    monkeypatch.setattr("app.scrap_portal.r2.public_url", bucket.public_url)
    monkeypatch.setattr("app.scrap_portal.r2.require_r2", bucket.require_r2)
    return bucket


def test_slave_login_and_admin_login(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    slave = client.post("/scrap/login", json={"token": "2001"})
    assert slave.status_code == 200
    assert slave.json() == {"ok": True, "role": "slave"}
    admin = client.post("/api/scrap/login", json={"token": "desk-token"})
    assert admin.status_code == 200
    assert admin.json()["role"] == "admin"
    assert client.post("/scrap/login", json={"token": "nope"}).status_code == 401


def test_creates_lists_and_deletes_a_scrap_in_the_bucket(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    headers = {"Authorization": "Bearer desk-token"}
    signed = client.post(
        "/scrap/get-upload-url",
        json={"key": "scraps/one.jpg", "contentType": "image/jpeg", "kind": "picture"},
        headers=headers,
    )
    assert signed.status_code == 200
    body = signed.json()
    assert body["object_key"] == "2001/scraps/one.jpg"
    assert body["upload_url"].endswith("/2001/scraps/one.jpg")
    assert "1001" not in signed.text
    assert "herb/" not in body["object_key"]

    created = client.post(
        "/scrap/scraps",
        json={
            "type": "bikes",
            "price": 18500,
            "pictures": [{"object_key": "scraps/one.jpg", "content_type": "image/jpeg", "filename": "one.jpg"}],
            "documents": [],
        },
        headers=headers,
    )
    assert created.status_code == 201
    scrap = created.json()["scrap"]
    assert scrap["portal"] == "2001"
    assert scrap["price"] == 18500
    assert scrap["pictures"][0]["url"] == "https://media.example/2001/scraps/one.jpg"
    assert "2001/catalog.json" in bucket.objects

    listed = client.get("/scrap/scraps")
    assert listed.status_code == 200
    assert listed.json()["scraps"][0]["type"] == "bikes"
    assert client.get("/scrap/types").json() == {"types": ["bikes"]}

    empty = client.post(
        "/scrap/scraps",
        json={"type": "bikes", "price": 1, "pictures": [], "documents": []},
        headers=headers,
    )
    assert empty.status_code == 400

    removed = client.delete(f"/scrap/scraps/{scrap['id']}", headers=headers)
    assert removed.status_code == 204
    assert client.get("/scrap/scraps").json() == {"scraps": []}


def test_rejects_herb_and_shorts_keys(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    headers = {"Authorization": "Bearer desk-token"}
    for key in ("herb/images/abc.jpg", "shorts/clip.mp4"):
        response = client.post(
            "/scrap/get-upload-url",
            json={"key": key, "contentType": "image/jpeg", "kind": "picture"},
            headers=headers,
        )
        assert response.status_code == 400
