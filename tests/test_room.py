import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.room import KEPT_DUMPS

client = TestClient(app)


class Cursor:
    def __init__(self, docs: list[dict]):
        self.docs = docs

    def sort(self, key: str, direction: int):
        self.docs.sort(key=lambda item: item.get(key, ""), reverse=direction < 0)
        return self

    def skip(self, count: int):
        return self.docs[count:]


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
        room = query.get("room")
        return Cursor([dict(item) for item in self.docs if item.get("room") == room])

    def find_one(self, query: dict, sort=None):
        matches = [item for item in self.docs if item.get("room") == query.get("room")]
        if sort:
            key, direction = sort[0]
            matches.sort(key=lambda item: item.get(key, ""), reverse=direction < 0)
        return matches[0] if matches else None

    def delete_many(self, query: dict) -> None:
        stale = set(query.get("_id", {}).get("$in", []))
        self.docs = [item for item in self.docs if item["_id"] not in stale]


def _picture(ident: str, at: str, products: str = "Reading copies") -> dict:
    return {
        "id": ident,
        "at": at,
        "plans": {"admin": {"title": "Hold"}, "user": {"title": "Work"}},
        "diary": {"admin": [], "user": []},
        "schedule": {
            "admin": [{"date": "2026-11-09", "time": "07:00", "centre": "Rohn Centre", "products": products}],
            "user": [{"date": "2026-11-09", "time": "07:30", "centre": "Rohn Centre", "products": "Morning notes"}],
        },
        "added": [{"date": "2026-11-18", "time": "10:00", "centre": "Rohn Centre", "products": "Snapshot tea"}],
        "coaches": [],
        "centre": {"name": "Rohn Centre", "rooms": []},
        "attendance": [],
        "boardNotes": [{"when": "9 Nov", "body": "Quiet hour"}],
    }


@pytest.fixture
def dumps(monkeypatch: pytest.MonkeyPatch) -> Collection:
    collection = Collection()
    monkeypatch.setattr("app.room.mongo_configured", lambda: True)
    monkeypatch.setattr("app.room.snapshots", lambda: collection)
    return collection


def test_room_requires_mongo(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MONGODB_URL", raising=False)
    response = client.post("/api/room", json={"room": "herbalife", "snapshot": _picture("a", "t")})
    assert response.status_code == 503
    assert client.get("/api/room").status_code == 503


def test_dump_rejects_a_picture_without_a_schedule(dumps: Collection):
    response = client.post("/api/room", json={"room": "herbalife", "snapshot": {"id": "a", "at": "t"}})
    assert response.status_code == 400
    assert dumps.docs == []


def test_dump_round_trip_keeps_schedule_fields(dumps: Collection):
    picture = _picture("one", "2026-11-13T12:00:00.000Z")
    saved = client.post("/api/room", json={"room": "herbalife", "snapshot": picture})
    assert saved.status_code == 200
    assert saved.json()["id"] == "one"

    loaded = client.get("/api/room", params={"room": "herbalife"})
    assert loaded.status_code == 200
    assert loaded.json()["snapshot"]["boardNotes"][0]["body"] == "Quiet hour"
    assert loaded.json()["snapshot"]["added"][0]["products"] == "Snapshot tea"

    schedules = client.get("/api/schedules", params={"room": "herbalife"}).json()
    assert schedules[0]["audience"] == "admin"
    assert schedules[-1] == {
        "id": "shared-2026-11-18-10:00",
        "room": "herbalife",
        "date": "2026-11-18",
        "time": "10:00",
        "centre": "Rohn Centre",
        "products": "Snapshot tea",
        "audience": "shared",
    }


def test_dumps_keep_the_newest_only(dumps: Collection):
    for index in range(KEPT_DUMPS + 3):
        stamp = f"2026-11-13T12:{index:02d}:00.000Z"
        response = client.post(
            "/api/room",
            json={"room": "herbalife", "snapshot": _picture(f"id-{index}", stamp)},
        )
        assert response.status_code == 200

    assert len(dumps.docs) == KEPT_DUMPS
    latest = client.get("/api/room").json()["snapshot"]["id"]
    assert latest == f"id-{KEPT_DUMPS + 2}"
    assert all(item["id"] != "id-0" for item in dumps.docs)
