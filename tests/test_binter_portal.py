"""Binter portal stores collects in herback's R2 bucket. No network."""

import json
import re
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

NOTIFIER = "notifier-number"
RECEIVER = "receiver-number"
ADMIN = "admin-number"
VEHICLE = "MH12AB1234"
OTHER_VEHICLE = "22BH1234AA"
KEY_RE = {
    "photo": re.compile(r"^binter/photos/[a-f0-9]{32}\.jpg$"),
    "clip": re.compile(r"^binter/clips/[a-f0-9]{32}\.mp4$"),
    "aadhaar": re.compile(r"^binter/documents/aadhaar/[a-f0-9]{32}\.jpg$"),
    "licence": re.compile(r"^binter/documents/licence/[a-f0-9]{32}\.png$"),
    "rc": re.compile(r"^binter/documents/rc/[a-f0-9]{32}\.pdf$"),
}


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.signed: list[str] = []
        self.deleted: list[str] = []

    def read_object(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def write_object(self, key: str, body: bytes, **_kwargs) -> None:
        self.objects[key] = body

    def delete_key(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def presign_put_key(self, key: str, content_type: str) -> str:
        self.signed.append(f"{key}:{content_type}")
        return f"https://example.r2.cloudflarestorage.com/{key}"

    def public_url(self, key: str) -> str:
        return f"https://media.example/{key}"

    def require_r2(self) -> None:
        return None


def _install(monkeypatch: pytest.MonkeyPatch) -> Bucket:
    bucket = Bucket()
    monkeypatch.setenv("BINTER_NOTIFIER_TOKEN", NOTIFIER)
    monkeypatch.setenv("BINTER_RECEIVER_TOKEN", RECEIVER)
    monkeypatch.setenv("BINTER_ADMIN_TOKEN", ADMIN)
    monkeypatch.setattr("app.binter_portal.r2.read_object", bucket.read_object)
    monkeypatch.setattr("app.binter_portal.r2.write_object", bucket.write_object)
    monkeypatch.setattr("app.binter_portal.r2.delete_key", bucket.delete_key)
    monkeypatch.setattr("app.binter_portal.r2.presign_put_key", bucket.presign_put_key)
    monkeypatch.setattr("app.binter_portal.r2.public_url", bucket.public_url)
    monkeypatch.setattr("app.binter_portal.r2.require_r2", bucket.require_r2)
    return bucket


def _auth(token: str, vehicle: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}"}
    if vehicle is not None:
        headers["X-Binter-Vehicle"] = vehicle
    return headers


def _uploaded(headers: dict[str, str], kind: str, content_type: str, filename: str, **extra) -> dict:
    response = client.post(
        "/binter/upload-url",
        json={"kind": kind, "contentType": content_type, "filename": filename, **extra},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"object_key": body["object_key"], "content_type": body["content_type"], "filename": filename}


def _papers(vehicle: str = VEHICLE) -> dict:
    headers = _auth(RECEIVER)
    return {
        "aadhaar": _uploaded(headers, "aadhaar", "image/jpeg", "aadhaar.jpg"),
        "licence": _uploaded(headers, "licence", "image/png", "licence.png"),
        "rc": _uploaded(headers, "rc", "application/pdf", "rc.pdf"),
    }


def _save_profile(
    vehicle: str = VEHICLE,
    name: str = "Ravi  O'Neil-Kumar",
    documents: dict | None = None,
    body_vehicle: str | None = None,
):
    return client.post(
        "/binter/profile",
        json={
            "name": name,
            "vehicle_number": body_vehicle if body_vehicle is not None else vehicle,
            "documents": _papers(vehicle) if documents is None else documents,
        },
        headers=_auth(RECEIVER, vehicle),
    )


def _collect(label: str = "North  yard, #2/B", bucket: str = "ferrous", clip: bool = False, path: str = "/binter/requests"):
    headers = _auth(NOTIFIER)
    photo = _uploaded(headers, "photo", "image/jpeg", "pile.jpg")
    body = {
        "bucket": bucket,
        "location_type": "yard",
        "location_label": label,
        "photo": photo,
    }
    if clip:
        body["clip"] = _uploaded(headers, "clip", "video/mp4", "pile.mp4")
    response = client.post(path, json=body, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["request"]


def _state(bucket: Bucket) -> dict:
    return json.loads(bucket.objects["binter/state.json"])


def _write_state(bucket: Bucket, state: dict) -> None:
    bucket.objects["binter/state.json"] = json.dumps(state).encode()


def test_three_logins_and_a_bad_login(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    notifier = client.post("/binter/login", json={"token": NOTIFIER})
    receiver = client.post("/binter/login", json={"token": f"  {RECEIVER}  "})
    admin = client.post("/api/binter/login", json={"token": ADMIN})
    assert notifier.status_code == 200
    assert notifier.json() == {"ok": True, "role": "notifier"}
    assert receiver.status_code == 200
    assert receiver.json() == {"ok": True, "role": "receiver"}
    assert admin.status_code == 200
    assert admin.json() == {"ok": True, "role": "admin"}

    bad = client.post("/binter/login", json={"token": "wrong-number"})
    assert bad.status_code == 401
    assert bad.json()["detail"] == "That number does not open the portal."
    for guess in ("2001", "1001", "admin", "password", ""):
        assert client.post("/binter/login", json={"token": guess}).status_code == 401

    def _boom(*_args):
        raise AssertionError("compare_digest ran")

    monkeypatch.setattr("app.binter_portal.hmac.compare_digest", _boom)
    assert client.post("/binter/login", json={"token": "no"}).status_code == 401
    monkeypatch.setenv("BINTER_ADMIN_TOKEN", "")
    assert client.post("/api/binter/login", json={"token": ADMIN}).status_code == 401

    closed = client.get("/binter/meta")
    assert closed.status_code == 401
    assert closed.json()["detail"] == "That number does not open the portal."


def test_health_flags_doors_without_echoing_the_numbers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BINTER_NOTIFIER_TOKEN", NOTIFIER)
    monkeypatch.setenv("BINTER_RECEIVER_TOKEN", RECEIVER)
    monkeypatch.setenv("BINTER_ADMIN_TOKEN", ADMIN)
    response = client.get("/health")
    body = response.json()
    assert body["binter_notifier_configured"] is True
    assert body["binter_receiver_configured"] is True
    assert body["binter_admin_configured"] is True
    assert NOTIFIER not in response.text
    assert RECEIVER not in response.text
    assert ADMIN not in response.text
    monkeypatch.setenv("BINTER_RECEIVER_TOKEN", "   ")
    assert client.get("/health").json()["binter_receiver_configured"] is False


def test_upload_keys_stay_under_binter_and_a_clip_rejects_an_image(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    photo = client.post(
        "/binter/upload-url",
        json={
            "kind": "photo",
            "contentType": "image/jpeg",
            "filename": "pile.jpg",
            "key": "herb/images/nope.jpg",
            "object_key": "shorts/nope.jpg",
        },
        headers=_auth(NOTIFIER),
    )
    assert photo.status_code == 200
    body = photo.json()
    assert set(body) == {
        "upload_url",
        "object_key",
        "content_type",
        "headers",
        "method",
        "max_bytes",
        "max_seconds",
    }
    assert KEY_RE["photo"].fullmatch(body["object_key"])
    assert body["object_key"].startswith("binter/")
    assert "herb/" not in body["object_key"]
    assert "2001/" not in body["object_key"]
    assert "shorts/" not in body["object_key"]
    assert body["upload_url"].endswith(body["object_key"])
    assert body["content_type"] == "image/jpeg"
    assert body["headers"] == {"content-type": "image/jpeg"}
    assert body["method"] == "PUT"
    assert body["max_bytes"] == 8 * 1024 * 1024
    assert body["max_seconds"] is None
    assert body["object_key"] not in {"herb/images/nope.jpg", "shorts/nope.jpg"}

    clip = client.post(
        "/binter/upload-url",
        json={"kind": "clip", "content_type": "image/jpeg", "filename": "pile.jpg"},
        headers=_auth(NOTIFIER),
    )
    assert clip.status_code == 400
    assert "image/jpeg" not in {item.split(":", 1)[1] for item in bucket.signed if "/clips/" in item}

    video = client.post(
        "/binter/upload-url",
        json={"kind": "clip", "content_type": "video/mp4", "filename": "pile.mp4"},
        headers=_auth(NOTIFIER),
    )
    assert video.status_code == 200
    assert KEY_RE["clip"].fullmatch(video.json()["object_key"])
    assert video.json()["max_seconds"] == 10
    assert video.json()["max_bytes"] == 20 * 1024 * 1024
    assert video.json()["headers"] == {"content-type": "video/mp4"}

    paper = _uploaded(_auth(RECEIVER), "aadhaar", "image/jpeg", "aadhaar.jpg")
    assert KEY_RE["aadhaar"].fullmatch(paper["object_key"])
    refused = client.post(
        "/binter/upload-url",
        json={"kind": "aadhaar", "contentType": "image/jpeg", "filename": "aadhaar.jpg"},
        headers=_auth(NOTIFIER),
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == "That number cannot do this."
    assert client.post(
        "/binter/upload-url",
        json={"kind": "photo", "contentType": "image/jpeg", "filename": "pile.jpg"},
        headers=_auth(ADMIN),
    ).status_code == 403


def test_meta_names_the_three_buckets(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    meta = client.get("/binter/meta", headers=_auth(NOTIFIER))
    assert meta.status_code == 200
    assert meta.json() == {
        "program": "Metals and glass",
        "buckets": [
            {"id": "ferrous", "name": "Ferrous metal", "note": "Steel, iron, and other magnetic scrap."},
            {
                "id": "nonferrous",
                "name": "Non-ferrous metal",
                "note": "Aluminium, copper, brass, and other metal that is not magnetic.",
            },
            {"id": "glass", "name": "Glass", "note": "Bottles, jars, and broken glass."},
        ],
        "locations": [
            {"id": "indoor", "name": "Indoor"},
            {"id": "yard", "name": "Yard"},
            {"id": "gate", "name": "Gate"},
        ],
    }


def test_notifier_creates_a_collect_and_receiver_without_papers_cannot_accept(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    created = _collect(path="/api/binter/requests")
    assert re.fullmatch(r"C\d{4}", created["collect_number"])
    assert created["status"] == "open"
    assert created["bucket"] == "ferrous"
    assert created["bucket_name"] == "Ferrous metal"
    assert created["location_type"] == "yard"
    assert created["location_name"] == "Yard"
    assert created["location_label"] == "North yard, #2/B"
    assert created["receiver"] is None
    assert created["clip"] is None
    assert created["accepted_at"] is None
    assert created["completed_at"] is None
    assert set(created["photo"]) == {"url", "content_type", "filename"}
    assert created["photo"]["url"].startswith("https://media.example/binter/photos/")
    assert "object_key" not in json.dumps(created)
    assert "binter/state.json" in bucket.objects

    refused = client.post(f"/binter/requests/{created['id']}/accept", headers=_auth(RECEIVER, VEHICLE))
    assert refused.status_code == 400
    assert refused.json()["detail"] == "Save the papers first."
    missing = client.post("/binter/requests/missing/accept", headers=_auth(RECEIVER, VEHICLE))
    assert missing.status_code == 404
    assert missing.json()["detail"] == "That collect is not in the bucket."


def test_profile_response_has_no_object_key_and_no_document_url(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    partial = _save_profile(documents={"aadhaar": _papers()["aadhaar"]})
    assert partial.status_code == 400
    assert client.get("/binter/profile", headers=_auth(RECEIVER, "MH-12-AB-1234")).status_code == 404
    assert client.get("/binter/profile", headers=_auth(RECEIVER, "MH-12-AB-1234")).json()["detail"] == (
        "No papers for that vehicle"
    )

    saved = _save_profile(body_vehicle="mh-12 ab 1234")
    assert saved.status_code == 200, saved.text
    assert "object_key" not in saved.text
    assert "https://" not in saved.text
    assert "http://" not in saved.text
    profile = saved.json()["profile"]
    assert set(profile) == {"name", "vehicle_number", "documents", "ready"}
    assert profile["name"] == "Ravi O'Neil-Kumar"
    assert profile["vehicle_number"] == VEHICLE
    assert profile["ready"] is True
    assert profile["documents"]["aadhaar"] == {"on_file": True, "filename": "aadhaar.jpg"}
    assert profile["documents"]["licence"] == {"on_file": True, "filename": "licence.png"}
    assert profile["documents"]["rc"] == {"on_file": True, "filename": "rc.pdf"}

    renamed = client.post(
        "/binter/profile",
        json={"name": "Ravi Kumar", "vehicle_number": VEHICLE},
        headers=_auth(RECEIVER, VEHICLE),
    )
    assert renamed.status_code == 200, renamed.text
    assert "object_key" not in renamed.text
    assert "https://" not in renamed.text
    assert renamed.json()["profile"]["documents"]["rc"]["filename"] == "rc.pdf"
    assert renamed.json()["profile"]["ready"] is True
    listed = client.get("/api/binter/profile", headers=_auth(RECEIVER, VEHICLE))
    assert listed.status_code == 200
    assert "object_key" not in listed.text
    assert "https://" not in listed.text


def test_accept_stores_the_vehicle_and_a_second_accept_is_409(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    saved = _save_profile(name="Ravi Kumar")
    assert saved.status_code == 200, saved.text
    created = _collect(label="Gate lane")
    accepted = client.post(f"/binter/requests/{created['id']}/accept", headers=_auth(RECEIVER, VEHICLE))
    assert accepted.status_code == 200, accepted.text
    body = accepted.json()["request"]
    assert body["status"] == "accepted"
    assert body["accepted_at"]
    assert body["receiver"] == {"name": "Ravi Kumar", "vehicle_number": VEHICLE}
    again = client.post(f"/api/binter/requests/{created['id']}/accept", headers=_auth(RECEIVER, VEHICLE))
    assert again.status_code == 409
    assert again.json()["detail"] == "That collect is already taken."


def test_the_same_vehicle_can_accept_two_open_collects(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    assert _save_profile(name="Ravi Kumar").status_code == 200
    first = _collect(label="Yard pile")
    second = _collect(label="Gate pile", bucket="glass")
    headers = _auth(RECEIVER, VEHICLE)
    assert client.post(f"/binter/requests/{first['id']}/accept", headers=headers).status_code == 200
    second_take = client.post(f"/binter/requests/{second['id']}/accept", headers=headers)
    assert second_take.status_code == 200, second_take.text
    assert second_take.json()["request"]["receiver"]["vehicle_number"] == VEHICLE
    listed = client.get("/binter/requests", headers=headers)
    ids = {item["id"] for item in listed.json()["requests"]}
    assert ids == {first["id"], second["id"]}
    assert {item["status"] for item in listed.json()["requests"]} == {"accepted"}


def test_only_the_accepting_vehicle_can_complete(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    assert _save_profile(name="Ravi Kumar").status_code == 200
    assert _save_profile(vehicle=OTHER_VEHICLE, name="Mina Das", body_vehicle="22 bh 1234 aa").status_code == 200
    taken = _collect(label="Indoor shed")
    left_open = _collect(label="Still there", bucket="nonferrous")
    assert client.post(f"/binter/requests/{taken['id']}/accept", headers=_auth(RECEIVER, VEHICLE)).status_code == 200

    other = client.post(
        f"/binter/requests/{taken['id']}/complete",
        headers=_auth(RECEIVER, OTHER_VEHICLE),
    )
    assert other.status_code == 403
    assert other.json()["detail"] == "That number cannot do this."

    done = client.post(f"/binter/requests/{taken['id']}/complete", headers=_auth(RECEIVER, "mh 12 ab 1234"))
    assert done.status_code == 200, done.text
    assert done.json()["request"]["status"] == "done"
    assert done.json()["request"]["completed_at"]
    assert done.json()["request"]["receiver"]["vehicle_number"] == VEHICLE

    receiver_rows = client.get("/binter/requests", headers=_auth(RECEIVER, VEHICLE)).json()["requests"]
    assert taken["id"] not in {item["id"] for item in receiver_rows}
    assert left_open["id"] in {item["id"] for item in receiver_rows}

    notifier_rows = client.get("/binter/requests", headers=_auth(NOTIFIER)).json()["requests"]
    finished = next(item for item in notifier_rows if item["id"] == taken["id"])
    assert finished["status"] == "done"
    assert finished["receiver"] == {"name": "Ravi Kumar", "vehicle_number": VEHICLE}
    admin_ids = {item["id"] for item in client.get("/binter/requests", headers=_auth(ADMIN)).json()["requests"]}
    assert taken["id"] in admin_ids


def test_a_request_older_than_thirty_days_drops_its_photo_and_keeps_the_profile(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    assert _save_profile(name="Ravi Kumar").status_code == 200
    fresh = _collect(label="Keep me")
    old = _collect(label="Old pile", clip=True)
    state = _state(bucket)
    old_row = next(item for item in state["requests"] if item["id"] == old["id"])
    photo_key = old_row["photo"]["object_key"]
    clip_key = old_row["clip"]["object_key"]
    old_row["created_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    bucket.objects[photo_key] = b"jpeg"
    bucket.objects[clip_key] = b"mp4"
    paper_keys = []
    for kind in ("aadhaar", "licence", "rc"):
        paper_keys.append(state["profiles"][VEHICLE]["documents"][kind]["object_key"])
        bucket.objects[paper_keys[-1]] = b"paper"
    _write_state(bucket, state)

    listed = client.get("/binter/requests", headers=_auth(NOTIFIER))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["requests"]] == [fresh["id"]]
    assert photo_key in bucket.deleted
    assert clip_key in bucket.deleted
    assert photo_key not in bucket.objects
    assert clip_key not in bucket.objects
    for key in paper_keys:
        assert key not in bucket.deleted
        assert key in bucket.objects
    profile = client.get("/binter/profile", headers=_auth(RECEIVER, VEHICLE))
    assert profile.status_code == 200
    assert profile.json()["profile"]["vehicle_number"] == VEHICLE
    assert profile.json()["profile"]["ready"] is True
    assert "object_key" not in profile.text


def test_delete_failure_keeps_the_expired_record(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    old = _collect(label="Stuck")
    state = _state(bucket)
    state["requests"][0]["created_at"] = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    _write_state(bucket, state)

    def _explode(key: str) -> None:
        raise RuntimeError(f"delete failed for {key}")

    monkeypatch.setattr("app.binter_portal.r2.delete_key", _explode)
    listed = client.get("/binter/requests", headers=_auth(NOTIFIER))
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["requests"]] == [old["id"]]
    assert old["id"] in _state(bucket)["requests"][0]["id"]


def test_gist_and_week_sheet_include_a_fresh_collect(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    created = _collect(label="This week", bucket="glass")
    admin = _auth(ADMIN)
    gist = client.get("/binter/gist", headers=admin)
    assert gist.status_code == 200, gist.text
    body = gist.json()
    assert set(body) == {"open", "accepted", "done", "by_bucket", "receivers", "week", "month"}
    assert body["open"] == 1
    assert body["accepted"] == 0
    assert body["done"] == 0
    assert body["by_bucket"] == {"ferrous": 0, "nonferrous": 0, "glass": 1}
    assert body["receivers"] == 0
    assert body["week"] == 1
    assert body["month"] == 1

    sheet = client.get("/binter/sheet", params={"span": "week"}, headers=admin)
    assert sheet.status_code == 200, sheet.text
    payload = sheet.json()
    assert payload["span"] == "week"
    start = datetime.fromisoformat(payload["from"])
    end = datetime.fromisoformat(payload["to"])
    assert start.weekday() == 0
    assert (start.hour, start.minute, start.second, start.microsecond) == (0, 0, 0, 0)
    assert start.utcoffset() == timedelta(hours=5, minutes=30)
    assert end - start == timedelta(days=7)
    assert [row["id"] for row in payload["rows"]] == [created["id"]]
    created_at = datetime.fromisoformat(created["created_at"])
    assert start <= created_at < end

    month = client.get("/api/binter/sheet", params={"span": "month"}, headers=admin)
    assert month.status_code == 200
    month_start = datetime.fromisoformat(month.json()["from"])
    assert month_start.day == 1
    assert month_start.hour == 0
    assert month_start.utcoffset() == timedelta(hours=5, minutes=30)
    assert month.json()["rows"][0]["id"] == created["id"]

    assert client.get("/binter/sheet", headers=admin).status_code == 400
    assert client.get("/binter/sheet", params={"span": "year"}, headers=admin).status_code == 400
    blocked = client.post(
        "/binter/requests",
        json={"bucket": "glass", "location_type": "gate", "location_label": "Side gate", "photo": {}},
        headers=admin,
    )
    assert blocked.status_code == 403
    assert blocked.json()["detail"] == "That number cannot do this."
    assert client.get("/binter/gist", headers=_auth(NOTIFIER)).status_code == 403


def test_cors_names_binter(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    monkeypatch.delenv("CORS_ORIGIN_REGEX", raising=False)
    from app.cors_config import load_cors_origin_regex, load_cors_origins

    origins = load_cors_origins()
    for origin in (
        "http://localhost:4200",
        "http://127.0.0.1:4200",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "https://herbant.vercel.app",
        "https://scrap-sooty.vercel.app",
        "https://binter.vercel.app",
    ):
        assert origin in origins
    assert load_cors_origin_regex() == r"^https://(?:herbant|scrap|binter)[a-z0-9-]*\.vercel\.app$"
    pattern = re.compile(load_cors_origin_regex() or "")
    assert pattern.fullmatch("https://binter.vercel.app")
    assert pattern.fullmatch("https://binter-git-abc.vercel.app")
    assert pattern.fullmatch("https://scrap-sooty.vercel.app")
    assert pattern.fullmatch("https://herbant.vercel.app")
    assert not pattern.fullmatch("https://evil.vercel.app")
