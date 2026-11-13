"""Sunskriti metals-and-glass collects stored in herback's R2 bucket.

The binter portal calls these routes. Photographs, clips, papers, and the
collect list live under ``binter/``. herbant media stays under ``herb/`` and
scrap stays under ``2001/``. ``BINTER_NOTIFIER_TOKEN``, ``BINTER_RECEIVER_TOKEN``,
and ``BINTER_ADMIN_TOKEN`` are the three doors. A blank value keeps that door shut.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Body, HTTPException, Request, status

from . import r2

router = APIRouter(prefix="/binter", tags=["binter"])

STATE_KEY = "binter/state.json"
RETENTION = timedelta(days=30)
KOLKATA = ZoneInfo("Asia/Kolkata")
_LOCK = threading.Lock()

DOOR_SHUT = "That number does not open the portal."
WRONG_ROLE = "That number cannot do this."
NO_PAPERS = "No papers for that vehicle"
ALREADY_TAKEN = "That collect is already taken."
PAPERS_FIRST = "Save the papers first."
NOT_ON_THE_WAY = "That collect is not on the way."
NOT_FOUND = "That collect is not in the bucket."

DOC_KINDS = ("aadhaar", "licence", "rc")
PHOTO_MAX_BYTES = 8 * 1024 * 1024
CLIP_MAX_BYTES = 20 * 1024 * 1024
DOCUMENT_MAX_BYTES = 8 * 1024 * 1024
CLIP_MAX_SECONDS = 10

BUCKETS = (
    {
        "id": "ferrous",
        "name": "Ferrous metal",
        "note": "Steel, iron, and other magnetic scrap.",
    },
    {
        "id": "nonferrous",
        "name": "Non-ferrous metal",
        "note": "Aluminium, copper, brass, and other metal that is not magnetic.",
    },
    {
        "id": "glass",
        "name": "Glass",
        "note": "Bottles, jars, and broken glass.",
    },
)
LOCATIONS = (
    {"id": "indoor", "name": "Indoor"},
    {"id": "yard", "name": "Yard"},
    {"id": "gate", "name": "Gate"},
)
_BUCKET_NAMES = {item["id"]: item["name"] for item in BUCKETS}
_LOCATION_NAMES = {item["id"]: item["name"] for item in LOCATIONS}

PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
CLIP_TYPES = {"video/mp4": ".mp4", "video/webm": ".webm", "video/quicktime": ".mov"}
DOCUMENT_TYPES = {**PHOTO_TYPES, "application/pdf": ".pdf"}
_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "application/x-pdf": "application/pdf",
    "video/mov": "video/quicktime",
    "application/mp4": "video/mp4",
}
_KIND_ROLE = {
    "photo": "notifier",
    "clip": "notifier",
    "aadhaar": "receiver",
    "licence": "receiver",
    "rc": "receiver",
}
_HEX = r"[a-f0-9]{32}"
_KEY = {
    "photo": re.compile(rf"^binter/photos/{_HEX}\.(jpg|png|webp)$"),
    "clip": re.compile(rf"^binter/clips/{_HEX}\.(mp4|webm|mov)$"),
    "aadhaar": re.compile(rf"^binter/documents/aadhaar/{_HEX}\.(jpg|png|webp|pdf)$"),
    "licence": re.compile(rf"^binter/documents/licence/{_HEX}\.(jpg|png|webp|pdf)$"),
    "rc": re.compile(rf"^binter/documents/rc/{_HEX}\.(jpg|png|webp|pdf)$"),
}
_VEHICLE_RE = re.compile(r"^(?:[A-Z]{2}\d{1,2}[A-Z]{1,3}\d{3,4}|\d{2}BH\d{4}[A-Z]{1,2})$")
_NAME_MARKS = " .'\u2019-"
_PLACE_MARKS = " .'\u2019-,#/"
_FOREIGN_PREFIXES = ("herb/", "2001/", "shorts/")


def _env_token(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _same(left: str, right: str) -> bool:
    if not left or not right or len(left) != len(right):
        return False
    left_bytes = left.encode()
    right_bytes = right.encode()
    if len(left_bytes) != len(right_bytes):
        return False
    return hmac.compare_digest(left_bytes, right_bytes)


def _role(token: str) -> str | None:
    if not token:
        return None
    doors = (
        ("notifier", "BINTER_NOTIFIER_TOKEN"),
        ("receiver", "BINTER_RECEIVER_TOKEN"),
        ("admin", "BINTER_ADMIN_TOKEN"),
    )
    for role, name in doors:
        if _same(token, _env_token(name)):
            return role
    return None


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    match = re.fullmatch(r"Bearer (\S+)", header.strip())
    return match.group(1) if match else ""


def _require(request: Request, *roles: str) -> str:
    role = _role(_bearer(request))
    if role is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DOOR_SHUT)
    if role not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)
    return role


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _marks_ok(value: str, marks: str) -> bool:
    return all(char.isalnum() or char in marks for char in value)


def _person_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="Name must be 2 to 80 characters.")
    name = _collapse(raw)
    if not 2 <= len(name) <= 80 or not _marks_ok(name, _NAME_MARKS):
        raise HTTPException(status_code=400, detail="Name must be 2 to 80 characters.")
    return name


def _place(raw: object) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="Place must be 2 to 60 characters.")
    label = _collapse(raw)
    if not 2 <= len(label) <= 60 or not _marks_ok(label, _PLACE_MARKS):
        raise HTTPException(status_code=400, detail="Place must be 2 to 60 characters.")
    return label


def _vehicle_number(raw: object) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="Vehicle number is not valid.")
    cleaned = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not _VEHICLE_RE.fullmatch(cleaned):
        raise HTTPException(status_code=400, detail="Vehicle number is not valid.")
    return cleaned


def _vehicle_header(request: Request) -> str:
    raw = request.headers.get("x-binter-vehicle")
    if raw is None:
        raise HTTPException(status_code=400, detail="Vehicle number is not valid.")
    return _vehicle_number(raw)


def _type_table(kind: str) -> dict[str, str]:
    if kind == "photo":
        return PHOTO_TYPES
    if kind == "clip":
        return CLIP_TYPES
    return DOCUMENT_TYPES


def _normalize_type(raw: object) -> str:
    text = str(raw or "").split(";")[0].strip().lower()
    return _ALIASES.get(text, text)


def _content_type_for(kind: str, raw: object) -> str:
    content_type = _normalize_type(raw)
    if content_type not in _type_table(kind):
        if kind == "clip":
            detail = "A clip must be a video."
        elif kind == "photo":
            detail = "A photograph must be an image."
        else:
            detail = "A paper must be an image or a PDF."
        raise HTTPException(status_code=400, detail=detail)
    return content_type


def _filename(raw: object, fallback: str) -> str:
    if not isinstance(raw, str):
        return fallback
    name = _collapse(raw.replace("\\", "/").split("/")[-1])
    if name in {"", ".", ".."} or "://" in name or name.lower().startswith("http"):
        return fallback
    return name[:80]


def _mint_key(kind: str, content_type: str) -> str:
    ext = _type_table(kind)[content_type]
    stem = uuid.uuid4().hex
    if kind == "photo":
        return f"binter/photos/{stem}{ext}"
    if kind == "clip":
        return f"binter/clips/{stem}{ext}"
    return f"binter/documents/{kind}/{stem}{ext}"


def _media(kind: str, raw: object) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Each file needs an object key")
    content_type = _content_type_for(kind, raw.get("content_type") or raw.get("contentType") or "")
    key = raw.get("object_key")
    if key is None:
        key = raw.get("objectKey")
    if key is None:
        key = raw.get("key")
    if not isinstance(key, str):
        raise HTTPException(status_code=400, detail="invalid key")
    key = key.strip()
    if key.startswith(_FOREIGN_PREFIXES) or not _KEY[kind].fullmatch(key):
        raise HTTPException(status_code=400, detail="invalid key")
    if not key.endswith(_type_table(kind)[content_type]):
        raise HTTPException(status_code=400, detail="invalid key")
    return {
        "object_key": key,
        "content_type": content_type,
        "filename": _filename(raw.get("filename"), f"{kind}{_type_table(kind)[content_type]}"),
    }


def _empty_state() -> dict:
    return {"profiles": {}, "requests": []}


def _read_state() -> dict:
    r2.require_r2()
    raw = r2.read_object(STATE_KEY)
    if not raw:
        return _empty_state()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return _empty_state()
    if not isinstance(parsed, dict):
        return _empty_state()
    profiles = parsed.get("profiles")
    requests = parsed.get("requests")
    if not isinstance(profiles, dict):
        profiles = {}
    if not isinstance(requests, list):
        requests = []
    return {
        "profiles": {str(key): value for key, value in profiles.items() if isinstance(value, dict)},
        "requests": [item for item in requests if isinstance(item, dict)],
    }


def _write_state(state: dict) -> None:
    r2.write_object(
        STATE_KEY,
        json.dumps({"profiles": state["profiles"], "requests": state["requests"]}).encode(),
        content_type="application/json",
        cache_control="no-store",
    )


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _expired(created_at: object, now: datetime) -> bool:
    created = _parse_time(created_at)
    if created is None:
        return False
    return now - created >= RETENTION


def _delete_request_media(item: dict) -> None:
    errors: list[Exception] = []
    for field, prefix in (("photo", "binter/photos/"), ("clip", "binter/clips/")):
        media = item.get(field)
        if not isinstance(media, dict):
            continue
        key = media.get("object_key")
        if not isinstance(key, str) or not key.startswith(prefix):
            continue
        try:
            r2.delete_key(key)
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[0]


def _sweep(state: dict, now: datetime) -> bool:
    kept: list[dict] = []
    dropped = False
    for item in state["requests"]:
        if not _expired(item.get("created_at"), now):
            kept.append(item)
            continue
        try:
            _delete_request_media(item)
        except Exception:
            kept.append(item)
            continue
        dropped = True
    state["requests"] = kept
    return dropped


def _with_state(update, *, save: bool):
    with _LOCK:
        state = _read_state()
        if _sweep(state, datetime.now(timezone.utc)):
            _write_state(state)
        result = update(state)
        if save:
            _write_state(state)
        return result


def _collect_number(requests: list[dict]) -> str:
    used = {item.get("collect_number") for item in requests}
    for _ in range(50):
        candidate = f"C{1000 + secrets.randbelow(9000)}"
        if candidate not in used:
            return candidate
    extra = "C" + uuid.uuid4().hex[:8]
    return extra


def _ready(profile: dict) -> bool:
    name = profile.get("name")
    vehicle = profile.get("vehicle_number")
    if not isinstance(name, str) or not name.strip():
        return False
    if not isinstance(vehicle, str) or not _VEHICLE_RE.fullmatch(vehicle):
        return False
    documents = profile.get("documents")
    if not isinstance(documents, dict):
        return False
    for kind in DOC_KINDS:
        entry = documents.get(kind)
        if not isinstance(entry, dict) or not isinstance(entry.get("object_key"), str) or not entry.get("object_key"):
            return False
    return True


def _public_profile(profile: dict) -> dict:
    documents = profile.get("documents") if isinstance(profile.get("documents"), dict) else {}
    public_docs = {}
    for kind in DOC_KINDS:
        entry = documents.get(kind)
        if isinstance(entry, dict) and entry.get("object_key"):
            public_docs[kind] = {"on_file": True, "filename": str(entry.get("filename") or "file")}
        else:
            public_docs[kind] = {"on_file": False, "filename": ""}
    return {
        "name": profile.get("name") or "",
        "vehicle_number": profile.get("vehicle_number") or "",
        "documents": public_docs,
        "ready": _ready(profile),
    }


def _public_media(media: object) -> dict | None:
    if not isinstance(media, dict):
        return None
    key = media.get("object_key")
    if not isinstance(key, str) or not key:
        return None
    return {
        "url": r2.public_url(key),
        "content_type": str(media.get("content_type") or ""),
        "filename": str(media.get("filename") or ""),
    }


def _public_receiver(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    vehicle = raw.get("vehicle_number")
    if not isinstance(name, str) or not isinstance(vehicle, str):
        return None
    return {"name": name, "vehicle_number": vehicle}


def _public_request(item: dict) -> dict:
    bucket_id = str(item.get("bucket") or "")
    location_id = str(item.get("location_type") or "")
    return {
        "id": item.get("id"),
        "collect_number": item.get("collect_number"),
        "bucket": bucket_id,
        "bucket_name": _BUCKET_NAMES.get(bucket_id, bucket_id),
        "location_type": location_id,
        "location_name": _LOCATION_NAMES.get(location_id, location_id),
        "location_label": item.get("location_label"),
        "photo": _public_media(item.get("photo")),
        "clip": _public_media(item.get("clip")),
        "status": item.get("status"),
        "created_at": item.get("created_at"),
        "accepted_at": item.get("accepted_at"),
        "completed_at": item.get("completed_at"),
        "receiver": _public_receiver(item.get("receiver")),
    }


def _find(state: dict, request_id: str) -> dict:
    for item in state["requests"]:
        if item.get("id") == request_id:
            return item
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


def _visible(item: dict, role: str, vehicle: str | None) -> bool:
    if role != "receiver":
        return True
    status_name = item.get("status")
    if status_name == "open":
        return True
    if status_name != "accepted" or not vehicle:
        return False
    receiver = item.get("receiver")
    return isinstance(receiver, dict) and receiver.get("vehicle_number") == vehicle


def _span_bounds(span: str, moment: datetime | None = None) -> tuple[datetime, datetime]:
    current = (moment or datetime.now(KOLKATA)).astimezone(KOLKATA)
    if span == "week":
        start = (current - timedelta(days=current.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def _in_span(created_at: object, span: str, moment: datetime | None = None) -> bool:
    created = _parse_time(created_at)
    if created is None:
        return False
    start, end = _span_bounds(span, moment)
    return start <= created < end


@router.post("/login")
def login(body: dict = Body(...)) -> dict:
    token = body.get("token") if isinstance(body, dict) else None
    token = token.strip() if isinstance(token, str) else ""
    role = _role(token)
    if not role:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DOOR_SHUT)
    return {"ok": True, "role": role}


@router.get("/meta")
def meta(request: Request) -> dict:
    _require(request, "notifier", "receiver", "admin")
    return {
        "program": "Metals and glass",
        "buckets": [dict(item) for item in BUCKETS],
        "locations": [dict(item) for item in LOCATIONS],
    }


@router.post("/upload-url")
def upload_url(request: Request, body: dict = Body(...)) -> dict:
    role = _require(request, "notifier", "receiver")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    kind = body.get("kind")
    if not isinstance(kind, str) or _KIND_ROLE.get(kind) != role:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)
    content_type = _content_type_for(kind, body.get("contentType") or body.get("content_type") or "")
    r2.require_r2()
    key = _mint_key(kind, content_type)
    upload = r2.presign_put_key(key, content_type)
    if kind == "clip":
        max_bytes = CLIP_MAX_BYTES
        max_seconds = CLIP_MAX_SECONDS
    elif kind == "photo":
        max_bytes = PHOTO_MAX_BYTES
        max_seconds = None
    else:
        max_bytes = DOCUMENT_MAX_BYTES
        max_seconds = None
    return {
        "upload_url": upload,
        "object_key": key,
        "content_type": content_type,
        "headers": {"content-type": content_type},
        "method": "PUT",
        "max_bytes": max_bytes,
        "max_seconds": max_seconds,
    }


@router.get("/profile")
def get_profile(request: Request) -> dict:
    _require(request, "receiver")
    vehicle = _vehicle_header(request)

    def read(state: dict) -> dict:
        profile = state["profiles"].get(vehicle)
        if not isinstance(profile, dict):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_PAPERS)
        return {"profile": _public_profile(profile)}

    return _with_state(read, save=False)


@router.post("/profile")
def save_profile(request: Request, body: dict = Body(...)) -> dict:
    _require(request, "receiver")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    vehicle = _vehicle_header(request)
    if _vehicle_number(body.get("vehicle_number")) != vehicle:
        raise HTTPException(status_code=400, detail="Vehicle number is not valid.")
    name = _person_name(body.get("name"))
    incoming = body.get("documents", {})
    if incoming is None:
        incoming = {}
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=400, detail="Save all three papers.")
    parsed: dict[str, dict] = {}
    for kind in DOC_KINDS:
        if kind not in incoming or incoming[kind] is None:
            continue
        parsed[kind] = _media(kind, incoming[kind])

    def write(state: dict) -> dict:
        current = state["profiles"].get(vehicle)
        documents: dict[str, dict] = {}
        if isinstance(current, dict) and isinstance(current.get("documents"), dict):
            for kind in DOC_KINDS:
                old = current["documents"].get(kind)
                if isinstance(old, dict) and isinstance(old.get("object_key"), str) and old.get("object_key"):
                    documents[kind] = {
                        "object_key": old["object_key"],
                        "content_type": str(old.get("content_type") or ""),
                        "filename": str(old.get("filename") or kind),
                    }
        documents.update(parsed)
        if any(kind not in documents for kind in DOC_KINDS):
            raise HTTPException(status_code=400, detail="Save all three papers.")
        profile = {
            "name": name,
            "vehicle_number": vehicle,
            "documents": {kind: documents[kind] for kind in DOC_KINDS},
        }
        state["profiles"][vehicle] = profile
        return {"profile": _public_profile(profile)}

    return _with_state(write, save=True)


@router.get("/requests")
def list_requests(request: Request) -> dict:
    role = _require(request, "notifier", "receiver", "admin")
    vehicle = _vehicle_header(request) if role == "receiver" else None

    def read(state: dict) -> dict:
        return {"requests": [_public_request(item) for item in state["requests"] if _visible(item, role, vehicle)]}

    return _with_state(read, save=False)


@router.post("/requests", status_code=status.HTTP_201_CREATED)
def create_request(request: Request, body: dict = Body(...)) -> dict:
    _require(request, "notifier")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    bucket = body.get("bucket")
    if bucket not in _BUCKET_NAMES:
        raise HTTPException(status_code=400, detail="Choose a bucket.")
    location_type = body.get("location_type")
    if location_type not in _LOCATION_NAMES:
        raise HTTPException(status_code=400, detail="Choose a location.")
    label = _place(body.get("location_label"))
    if body.get("photo") is None:
        raise HTTPException(status_code=400, detail="A photograph is required.")
    photo = _media("photo", body.get("photo"))
    clip = None if body.get("clip") is None else _media("clip", body.get("clip"))

    def write(state: dict) -> dict:
        item = {
            "id": uuid.uuid4().hex,
            "collect_number": _collect_number(state["requests"]),
            "bucket": bucket,
            "location_type": location_type,
            "location_label": label,
            "photo": photo,
            "clip": clip,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "accepted_at": None,
            "completed_at": None,
            "receiver": None,
        }
        state["requests"].insert(0, item)
        return {"request": _public_request(item)}

    return _with_state(write, save=True)


@router.post("/requests/{request_id}/accept")
def accept_request(request_id: str, request: Request) -> dict:
    _require(request, "receiver")
    vehicle = _vehicle_header(request)

    def write(state: dict) -> dict:
        item = _find(state, request_id)
        if item.get("status") != "open":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=ALREADY_TAKEN)
        profile = state["profiles"].get(vehicle)
        if not isinstance(profile, dict) or not _ready(profile):
            raise HTTPException(status_code=400, detail=PAPERS_FIRST)
        item["status"] = "accepted"
        item["accepted_at"] = datetime.now(timezone.utc).isoformat()
        item["receiver"] = {"name": profile["name"], "vehicle_number": profile["vehicle_number"]}
        return {"request": _public_request(item)}

    return _with_state(write, save=True)


@router.post("/requests/{request_id}/complete")
def complete_request(request_id: str, request: Request) -> dict:
    _require(request, "receiver")
    vehicle = _vehicle_header(request)

    def write(state: dict) -> dict:
        item = _find(state, request_id)
        if item.get("status") != "accepted":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=NOT_ON_THE_WAY)
        receiver = item.get("receiver") if isinstance(item.get("receiver"), dict) else {}
        if receiver.get("vehicle_number") != vehicle:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)
        item["status"] = "done"
        item["completed_at"] = datetime.now(timezone.utc).isoformat()
        return {"request": _public_request(item)}

    return _with_state(write, save=True)


@router.get("/gist")
def gist(request: Request) -> dict:
    _require(request, "admin")

    def read(state: dict) -> dict:
        moment = datetime.now(KOLKATA)
        counts = {"open": 0, "accepted": 0, "done": 0}
        by_bucket = {bucket_id: 0 for bucket_id in _BUCKET_NAMES}
        week = 0
        month = 0
        for item in state["requests"]:
            status_name = item.get("status")
            if status_name in counts:
                counts[status_name] += 1
            bucket = item.get("bucket")
            if bucket in by_bucket:
                by_bucket[bucket] += 1
            if _in_span(item.get("created_at"), "week", moment):
                week += 1
            if _in_span(item.get("created_at"), "month", moment):
                month += 1
        receivers = sum(1 for profile in state["profiles"].values() if isinstance(profile, dict) and _ready(profile))
        return {
            "open": counts["open"],
            "accepted": counts["accepted"],
            "done": counts["done"],
            "by_bucket": by_bucket,
            "receivers": receivers,
            "week": week,
            "month": month,
        }

    return _with_state(read, save=False)


@router.get("/sheet")
def sheet(request: Request, span: str | None = None) -> dict:
    _require(request, "admin")
    if span not in {"week", "month"}:
        raise HTTPException(status_code=400, detail="Span must be week or month.")

    def read(state: dict) -> dict:
        start, end = _span_bounds(span)
        rows = []
        for item in state["requests"]:
            created = _parse_time(item.get("created_at"))
            if created is not None and start <= created < end:
                rows.append(_public_request(item))
        rows.sort(key=lambda row: row.get("created_at") or "", reverse=True)
        return {"span": span, "from": start.isoformat(), "to": end.isoformat(), "rows": rows}

    return _with_state(read, save=False)
