"""Repeated portal flows. Each step is one request, and a repeat does not invent another."""

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.room import KEPT_DUMPS

client = TestClient(app)


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.writes = 0
        self.reads = 0

    def read_object(self, key: str) -> bytes | None:
        self.reads += 1
        return self.objects.get(key)

    def write_object(self, key: str, body: bytes, **_kwargs) -> None:
        self.writes += 1
        self.objects[key] = body

    def delete_key(self, key: str) -> None:
        self.objects.pop(key, None)

    def presign_put_key(self, key: str, content_type: str) -> str:
        return f"https://example.r2.cloudflarestorage.com/{key}?type={content_type}"

    def public_url(self, key: str) -> str:
        return f"https://media.example/{key}"

    def require_r2(self) -> None:
        return None


class Collection:
    def __init__(self) -> None:
        self.docs: list[dict] = []
        self._seq = 0

    def create_index(self, *_args, **_kwargs) -> None:
        return None

    def insert_one(self, document: dict) -> None:
        self._seq += 1
        self.docs.append({**document, "_id": self._seq})

    def find(self, query: dict, _projection: dict | None = None):
        return _Cursor([dict(item) for item in self.docs if item.get("room") == query.get("room")])

    def find_one(self, query: dict, sort=None):
        matches = [item for item in self.docs if item.get("room") == query.get("room")]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda item: item.get(key, ""), reverse=direction < 0)
        return matches[0] if matches else None

    def delete_many(self, query: dict) -> None:
        stale = set(query.get("_id", {}).get("$in", []))
        self.docs = [item for item in self.docs if item["_id"] not in stale]


class _Cursor:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def sort(self, key: str, direction: int):
        self.docs.sort(key=lambda item: item.get(key, ""), reverse=direction < 0)
        return self

    def skip(self, count: int):
        return self.docs[count:]


def _picture(ident: str, at: str) -> dict:
    return {
        "id": ident,
        "at": at,
        "plans": {"admin": {"title": "Hold"}, "user": {"title": "Work"}},
        "diary": {"admin": [], "user": []},
        "schedule": {
            "admin": [{"date": "2026-11-09", "time": "07:00", "centre": "Rohn Centre", "products": "Reading copies"}],
            "user": [{"date": "2026-11-09", "time": "07:30", "centre": "Rohn Centre", "products": "Morning notes"}],
        },
        "added": [{"date": "2026-11-18", "time": "10:00", "centre": "Rohn Centre", "products": "Snapshot tea"}],
        "coaches": [],
        "centre": {"name": "Rohn Centre", "rooms": []},
        "attendance": [],
        "boardNotes": [],
    }


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch) -> Bucket:
    store = Bucket()
    monkeypatch.setenv("BINTER_NOTIFIER_TOKEN", "notifier-number")
    monkeypatch.setenv("BINTER_RECEIVER_TOKEN", "receiver-number")
    monkeypatch.setenv("BINTER_ADMIN_TOKEN", "admin-number")
    monkeypatch.setenv("UPLOAD_TOKEN", "desk-token")
    monkeypatch.setenv("HERB_API_KEY", "test-key")
    for module in ("app.binter_portal", "app.scrap_portal"):
        monkeypatch.setattr(f"{module}.r2.read_object", store.read_object)
        monkeypatch.setattr(f"{module}.r2.write_object", store.write_object)
        monkeypatch.setattr(f"{module}.r2.delete_key", store.delete_key)
        monkeypatch.setattr(f"{module}.r2.presign_put_key", store.presign_put_key)
        monkeypatch.setattr(f"{module}.r2.public_url", store.public_url)
        monkeypatch.setattr(f"{module}.r2.require_r2", store.require_r2)
    return store


def _auth(token: str, vehicle: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if vehicle:
        headers["X-Binter-Vehicle"] = vehicle
    return headers


def test_binter_collect_advances_once_and_a_repeat_stops(bucket: Bucket):
    assert client.post("/binter/login", json={"token": "nope"}).status_code == 401
    assert bucket.writes == 0

    roles = []
    for _ in range(12):
        roles.append(client.post("/binter/login", json={"token": "notifier-number"}).json()["role"])
        roles.append(client.post("/binter/login", json={"token": "receiver-number"}).json()["role"])
        roles.append(client.post("/binter/login", json={"token": "admin-number"}).json()["role"])
    assert roles[0::3] == ["notifier"] * 12
    assert roles[1::3] == ["receiver"] * 12
    assert roles[2::3] == ["admin"] * 12
    assert bucket.writes == 0

    receiver = _auth("receiver-number", "MH12AB1234")
    papers = {}
    for kind, content_type, filename in (
        ("aadhaar", "image/jpeg", "aadhaar.jpg"),
        ("licence", "image/png", "licence.png"),
        ("rc", "application/pdf", "rc.pdf"),
    ):
        signed = client.post(
            "/binter/upload-url",
            json={"kind": kind, "contentType": content_type, "filename": filename},
            headers=_auth("receiver-number"),
        )
        assert signed.status_code == 200
        papers[kind] = {
            "object_key": signed.json()["object_key"],
            "content_type": signed.json()["content_type"],
            "filename": filename,
        }
    saved = client.post(
        "/binter/profile",
        json={"name": "Ravi Kumar", "vehicle_number": "MH12AB1234", "documents": papers},
        headers=receiver,
    )
    assert saved.status_code == 200
    assert bucket.writes == 1

    notifier = _auth("notifier-number")
    created_ids = []
    for index in range(8):
        photo = client.post(
            "/binter/upload-url",
            json={"kind": "photo", "contentType": "image/jpeg", "filename": f"pile-{index}.jpg"},
            headers=notifier,
        )
        assert photo.status_code == 200
        made = client.post(
            "/binter/requests",
            json={
                "bucket": "glass" if index % 2 else "ferrous",
                "location_type": "yard",
                "location_label": f"Bay {index}",
                "photo": {
                    "object_key": photo.json()["object_key"],
                    "content_type": "image/jpeg",
                    "filename": f"pile-{index}.jpg",
                },
                "clip": None,
            },
            headers=notifier,
        )
        assert made.status_code == 201
        created_ids.append(made.json()["request"]["id"])
    assert bucket.writes == 9

    listed = client.get("/binter/requests", headers=notifier).json()["requests"]
    assert [row["status"] for row in listed] == ["open"] * 8
    assert bucket.writes == 9

    ident = created_ids[0]
    accepted = client.post(f"/binter/requests/{ident}/accept", headers=receiver)
    assert accepted.status_code == 200
    assert accepted.json()["request"]["status"] == "accepted"
    again = client.post(f"/binter/requests/{ident}/accept", headers=receiver)
    assert again.status_code == 409
    assert bucket.writes == 10

    done = client.post(f"/binter/requests/{ident}/complete", headers=receiver)
    assert done.status_code == 200
    assert done.json()["request"]["status"] == "done"
    repeat = client.post(f"/binter/requests/{ident}/complete", headers=receiver)
    assert repeat.status_code == 409
    assert bucket.writes == 11

    visible = client.get("/binter/requests", headers=receiver).json()["requests"]
    assert ident not in {row["id"] for row in visible}
    assert all(row["status"] == "open" for row in visible)

    for _ in range(10):
        assert client.get("/binter/requests", headers=notifier).status_code == 200
        assert client.get("/binter/gist", headers=_auth("admin-number")).status_code == 200
        assert client.get("/binter/sheet", params={"span": "week"}, headers=_auth("admin-number")).status_code == 200
    assert bucket.writes == 11


def test_scrap_and_room_reads_do_not_multiply(bucket: Bucket, monkeypatch: pytest.MonkeyPatch):
    for _ in range(15):
        slave = client.post("/api/scrap/login", json={"token": "2001"})
        assert slave.status_code == 200
        assert slave.json()["role"] == "slave"
        assert client.post("/scrap/login", json={"token": "nope"}).status_code == 401
    assert client.get("/api/scrap/scraps").json() == {"scraps": []}
    assert bucket.writes == 0

    collection = Collection()
    monkeypatch.setattr("app.room.mongo_configured", lambda: True)
    monkeypatch.setattr("app.room.snapshots", lambda: collection)
    first = None
    for index in range(KEPT_DUMPS + 6):
        saved = client.post(
            "/api/room",
            json={"room": "herbalife", "snapshot": _picture(f"id-{index}", f"2026-11-13T12:{index:02d}:00.000Z")},
        )
        assert saved.status_code == 200
        first = saved
    assert first is not None
    assert len(collection.docs) == KEPT_DUMPS
    bodies = [client.get("/api/schedules", params={"room": "herbalife"}).json() for _ in range(12)]
    assert bodies[1:] == bodies[:-1]
    assert len(bodies[0]) == 3
    assert {row["audience"] for row in bodies[0]} == {"admin", "user", "shared"}


def test_media_signs_are_one_key_each(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("R2_ACCOUNT_ID", "abc123")
    monkeypatch.setenv("R2_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("R2_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("R2_BUCKET", "herb-media")
    monkeypatch.setenv("R2_PUBLIC_BASE_URL", "https://media.example")
    monkeypatch.setenv("HERB_API_KEY", "test-key")
    keys = []
    for place in ("schedule", "coach", "centre"):
        for _ in range(4):
            signed = client.post(
                "/api/media/sign",
                json={"kind": "video", "place": place, "content_type": "video/mp4", "filename": "clip.mp4"},
                headers={"Authorization": "Bearer 1001"},
            )
            assert signed.status_code == 200
            body = signed.json()
            assert body["upload_method"] == "PUT"
            assert body["max_bytes"] == 40 * 1024 * 1024
            assert body["object_key"].startswith(f"herb/videos/{place}/")
            keys.append(body["object_key"])
            refused = client.post(
                "/api/media/sign",
                json={"kind": "image", "place": place, "content_type": "image/jpeg"},
                headers={"Authorization": "Bearer 2001"},
            )
            assert refused.status_code == 401
    assert len(keys) == len(set(keys))
