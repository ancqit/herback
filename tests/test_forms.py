"""Mongo holds the form and its CDN URLs. A bucket miss does not sit on the list."""

import json

import pytest

from app import forms


class Collection:
    def __init__(self) -> None:
        self.docs: dict[str, dict] = {}

    def create_index(self, *_args, **_kwargs) -> None:
        return None

    def find_one(self, query: dict, _projection: dict | None = None):
        document = self.docs.get(query["name"])
        return None if document is None else dict(document)

    def update_one(self, query: dict, update: dict, upsert: bool = False) -> None:
        name = query["name"]
        current = dict(self.docs.get(name) or {"name": name})
        current.update(update["$set"])
        self.docs[name] = current


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.reads = 0
        self.fail = False

    def read_object(self, key: str) -> bytes | None:
        self.reads += 1
        if self.fail:
            raise RuntimeError("bucket fetch failed")
        return self.objects.get(key)

    def public_url(self, key: str) -> str:
        return f"https://media.example/{key}"


def _install(monkeypatch: pytest.MonkeyPatch) -> tuple[Collection, Bucket]:
    forms._next_import.clear()
    collection = Collection()
    bucket = Bucket()
    monkeypatch.setattr(forms, "mongo_configured", lambda: True)
    monkeypatch.setattr(forms, "_collection", lambda: collection)
    monkeypatch.setattr(forms.r2, "read_object", bucket.read_object)
    monkeypatch.setattr(forms.r2, "public_url", bucket.public_url)
    return collection, bucket


def test_an_old_catalog_is_imported_once_and_then_read_from_mongo(monkeypatch: pytest.MonkeyPatch):
    collection, bucket = _install(monkeypatch)
    bucket.objects["2001/catalog.json"] = json.dumps(
        [
            {
                "id": "one",
                "pictures": [
                    {
                        "object_key": "2001/scraps/one.jpg",
                        "url": "https://media.example/2001/scraps/one.jpg",
                    }
                ],
            }
        ]
    ).encode()

    first = forms.adopt_bucket("scrap", "2001/catalog.json", {"scraps": []}, forms.scrap_form)
    second = forms.adopt_bucket("scrap", "2001/catalog.json", {"scraps": []}, forms.scrap_form)

    picture = first["scraps"][0]["pictures"][0]
    assert picture["cdn"] == "https://media.example/2001/scraps/one.jpg"
    assert picture["url"] == picture["cdn"]
    assert second["scraps"][0]["id"] == "one"
    assert bucket.reads == 1
    assert collection.docs["scrap"]["form"]["scraps"][0]["pictures"][0]["cdn"] == picture["cdn"]


def test_a_bucket_failure_returns_the_empty_form_without_retrying_immediately(monkeypatch: pytest.MonkeyPatch):
    _collection, bucket = _install(monkeypatch)
    bucket.fail = True

    first = forms.adopt_bucket("binter", "binter/state.json", {"profiles": {}, "requests": []}, forms.binter_form)
    second = forms.adopt_bucket("binter", "binter/state.json", {"profiles": {}, "requests": []}, forms.binter_form)

    assert first == {"profiles": {}, "requests": []}
    assert second == {"profiles": {}, "requests": []}
    assert bucket.reads == 1


def test_playback_uses_the_stored_cdn_url():
    media = {"object_key": "binter/clips/abc.mp4", "cdn": "https://files.example/binter/clips/abc.mp4"}
    assert forms.playback_url(media) == "https://files.example/binter/clips/abc.mp4"
