"""Portal forms in Mongo. The bytes stay in R2.

junctionBack stores `cdn` on a product after the image is published, and a
later read uses that URL. These forms do the same for pictures and videos:
the browser PUTs the file to the bucket, and the form kept here already has
the public CDN URL. A list is one document read. Playback does not fetch the
object.
"""

from __future__ import annotations

import copy
import json
import time
from datetime import datetime, timezone

from fastapi import HTTPException, status

from . import r2
from .database import get_database, mongo_configured

_IMPORT_PAUSE_SECONDS = 60
_next_import: dict[str, float] = {}


def require_mongo() -> None:
    if not mongo_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MONGODB_URL is not set")


def _collection():
    require_mongo()
    collection = get_database()["portal_forms"]
    collection.create_index("name", unique=True)
    return collection


def load_form(name: str) -> dict | None:
    document = _collection().find_one({"name": name}, {"form": 1})
    if document is None:
        return None
    form = document.get("form")
    return copy.deepcopy(form) if isinstance(form, dict) else None


def save_form(name: str, form: dict) -> None:
    saved_at = datetime.now(timezone.utc).isoformat()
    _collection().update_one(
        {"name": name},
        {"$set": {"name": name, "form": copy.deepcopy(form), "saved_at": saved_at}},
        upsert=True,
    )


def remember_cdn(record: dict, key: str) -> dict:
    """Stamp the public CDN URL. This does not read the bucket."""
    url = r2.public_url(key)
    record["cdn"] = url
    record["url"] = url
    return record


def playback_url(media: dict) -> str:
    """The URL a screen plays. The stored CDN URL wins over building one."""
    for field in ("cdn", "url"):
        value = media.get(field)
        if isinstance(value, str) and value.startswith("http"):
            return value
    key = media.get("object_key")
    if isinstance(key, str) and key:
        return r2.public_url(key)
    return ""


def _fill_cdn(value: object) -> object:
    if isinstance(value, dict):
        filled = {key: _fill_cdn(item) for key, item in value.items()}
        object_key = filled.get("object_key")
        if not isinstance(object_key, str) or not object_key:
            return filled
        cdn = filled.get("cdn")
        url = filled.get("url")
        if isinstance(cdn, str) and cdn.startswith("http"):
            if not (isinstance(url, str) and url.startswith("http")):
                filled["url"] = cdn
            return filled
        if isinstance(url, str) and url.startswith("http"):
            filled["cdn"] = url
            return filled
        try:
            stamped = r2.public_url(object_key)
        except Exception:
            return filled
        filled["cdn"] = stamped
        filled["url"] = stamped
        return filled
    if isinstance(value, list):
        return [_fill_cdn(item) for item in value]
    return value


def scrap_form(parsed: object) -> dict | None:
    if isinstance(parsed, list):
        scraps = parsed
    elif isinstance(parsed, dict) and isinstance(parsed.get("scraps"), list):
        scraps = parsed["scraps"]
    else:
        return None
    return _fill_cdn({"scraps": [item for item in scraps if isinstance(item, dict)]})


def binter_form(parsed: object) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    profiles = parsed.get("profiles")
    requests = parsed.get("requests")
    if not isinstance(profiles, dict):
        profiles = {}
    if not isinstance(requests, list):
        requests = []
    form = {
        "profiles": {str(key): value for key, value in profiles.items() if isinstance(value, dict)},
        "requests": [item for item in requests if isinstance(item, dict)],
    }
    return _fill_cdn(form)


def cart_form(parsed: object) -> dict | None:
    if not isinstance(parsed, dict):
        return None
    carts = parsed.get("carts")
    orders = parsed.get("orders")
    if not isinstance(carts, list):
        carts = []
    if not isinstance(orders, list):
        orders = []
    form = {
        "carts": [item for item in carts if isinstance(item, dict)],
        "orders": [item for item in orders if isinstance(item, dict)],
    }
    return _fill_cdn(form)


def _due(name: str) -> bool:
    return time.monotonic() >= _next_import.get(name, 0)


def _pause(name: str) -> None:
    _next_import[name] = time.monotonic() + _IMPORT_PAUSE_SECONDS


def adopt_bucket(name: str, bucket_key: str, empty: dict, normalize) -> dict:
    """Mongo first. One quiet import from the old bucket file when it is readable.

    A bucket error waits a minute before the next try, so a list stays a Mongo
    read while R2 is failing.
    """
    saved = load_form(name)
    if isinstance(saved, dict):
        return saved
    if not _due(name):
        return copy.deepcopy(empty)
    try:
        raw = r2.read_object(bucket_key)
    except Exception:
        _pause(name)
        return copy.deepcopy(empty)
    if not raw:
        return copy.deepcopy(empty)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return copy.deepcopy(empty)
    form = normalize(parsed)
    if not isinstance(form, dict):
        return copy.deepcopy(empty)
    save_form(name, form)
    loaded = load_form(name)
    return loaded if isinstance(loaded, dict) else copy.deepcopy(form)
