"""Pictures, clips, and papers are read back from the bucket for every portal."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


@pytest.fixture
def bucket(monkeypatch: pytest.MonkeyPatch):
    objects: dict[str, bytes] = {}

    def read_object(key: str) -> bytes | None:
        return objects.get(key)

    monkeypatch.setattr("app.r2.read_object", read_object)
    return objects


def test_each_portal_file_is_served_with_its_type(bucket: dict[str, bytes]):
    herb = "herb/audio/" + "a" * 32 + ".webm"
    clip = "binter/clips/" + "b" * 32 + ".mp4"
    paper = "binter/documents/aadhaar/" + "c" * 32 + ".pdf"
    bucket[herb] = b"audio"
    bucket[clip] = b"video"
    bucket[paper] = b"%PDF"

    audio = client.get(f"/files/{herb}")
    assert audio.status_code == 200
    assert audio.headers["content-type"].startswith("audio/webm")
    assert audio.content == b"audio"

    video = client.get(f"/api/files/{clip}")
    assert video.status_code == 200
    assert video.headers["content-type"].startswith("video/mp4")
    assert video.content == b"video"

    pdf = client.get(f"/files/{paper}")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"].startswith("application/pdf")
    assert pdf.content == b"%PDF"


def test_catalog_and_cart_state_stay_unread(bucket: dict[str, bytes]):
    bucket["2001/catalog.json"] = b'{"scraps":[]}'
    bucket["cart/state.json"] = b'{"carts":[]}'
    assert client.get("/files/2001/catalog.json").status_code == 404
    assert client.get("/files/cart/state.json").status_code == 404
    assert client.get("/files/herb/images/nope.jpg").status_code == 404
    assert client.get("/files/" + "herb/images/" + "d" * 32 + ".jpg").status_code == 404


def test_room_playback_points_at_the_bucket_file():
    from app.room import stamp_cdn

    key = "herb/videos/centre/" + "e" * 32 + ".mp4"
    picture = {"objectKey": key, "url": f"https://media.example/{key}"}
    stamp_cdn(picture)
    assert picture["cdn"] == f"https://herback-1.onrender.com/files/{key}"
