"""Scrap cards stored in herback's R2 bucket.

The scrap portal calls these routes. Pictures, documents, and the card list
live under ``2001/``. herbant media stays under ``herb/``. Credentials are the
same ``R2_*`` variables. ``UPLOAD_TOKEN`` is the desk login. ``2001`` is read-only.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Body, HTTPException, Request, Response, status

from . import r2

router = APIRouter(prefix="/scrap", tags=["scrap"])

CATALOG_KEY = "2001/catalog.json"
SLAVE = "2001"
PICTURE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
DOCUMENT_TYPES = PICTURE_TYPES | {"application/pdf"}
SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
TYPE_PATTERN = re.compile(r"^[\w]+(?:[ '\-][\w]+)*$", re.UNICODE)


def _token() -> str:
    return (os.getenv("UPLOAD_TOKEN") or "").strip()


def _same(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def _role(token: str) -> str | None:
    if _same(token, SLAVE):
        return "slave"
    expected = _token()
    if expected and _same(token, expected):
        return "admin"
    return None


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    match = re.fullmatch(r"Bearer (\S+)", header.strip())
    return match.group(1) if match else ""


def _require_admin(request: Request) -> None:
    if _role(_bearer(request)) != "admin":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Upload token is required")


def _public_key(raw: object) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail="invalid key")
    parts = [part for part in raw.strip().replace("\\", "/").split("/") if part]
    if not parts or len(parts) > 16:
        raise HTTPException(status_code=400, detail="invalid key")
    if any(part in {".", ".."} or not SEGMENT.match(part) for part in parts):
        raise HTTPException(status_code=400, detail="invalid key")
    if parts[0] in {"shorts", "herb"}:
        raise HTTPException(status_code=400, detail="invalid key")
    if parts[0] == "1001":
        parts[0] = SLAVE
    elif parts[0] != SLAVE:
        parts.insert(0, SLAVE)
    key = "/".join(parts)
    if key == CATALOG_KEY:
        raise HTTPException(status_code=400, detail="invalid key")
    return key


def _load() -> list[dict]:
    r2.require_r2()
    raw = r2.read_object(CATALOG_KEY)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, dict)]


def _save(scraps: list[dict]) -> None:
    r2.write_object(
        CATALOG_KEY,
        json.dumps(scraps).encode(),
        content_type="application/json",
        cache_control="no-store",
    )


def _files(raw: object, label: str, limit: int) -> list[dict]:
    if not isinstance(raw, list) or len(raw) > limit:
        raise HTTPException(status_code=400, detail=f"A scrap can have at most {limit} {label}")
    allowed = DOCUMENT_TYPES if label == "documents" else PICTURE_TYPES
    files: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail="Each file needs an object key")
        key = _public_key(entry.get("object_key") or entry.get("objectKey") or entry.get("key"))
        content_type = str(entry.get("content_type") or entry.get("contentType") or "").split(";")[0].strip().lower()
        if content_type not in allowed:
            message = "A document must be a PDF or an image" if label == "documents" else "A picture must be an image"
            raise HTTPException(status_code=400, detail=message)
        filename = str(entry.get("filename") or "file").split("/")[-1].split("\\")[-1].strip()[:80] or "file"
        files.append(
            {
                "object_key": key,
                "url": r2.public_url(key),
                "content_type": content_type,
                "filename": filename,
            }
        )
    return files


@router.post("/login")
def login(body: dict = Body(...)) -> dict:
    token = body.get("token") if isinstance(body, dict) else None
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise HTTPException(status_code=400, detail="Login is required")
    role = _role(token)
    if not role:
        raise HTTPException(status_code=401, detail="Login failed")
    return {"ok": True, "role": role}


@router.get("/types")
def types() -> dict:
    names = []
    for scrap in _load():
        name = scrap.get("type")
        if isinstance(name, str) and name and name != "bikes":
            names.append(name)
    return {"types": ["bikes", *dict.fromkeys(names)]}


@router.get("/scraps")
def list_scraps() -> dict:
    return {"scraps": _load()}


@router.post("/get-upload-url")
def upload_url(request: Request, body: dict = Body(...)) -> dict:
    _require_admin(request)
    r2.require_r2()
    kind = body.get("kind") if isinstance(body, dict) else None
    if kind not in {"picture", "document"}:
        kind = "picture"
    content_type = str(body.get("contentType") or body.get("content_type") or "").split(";")[0].strip().lower()
    allowed = DOCUMENT_TYPES if kind == "document" else PICTURE_TYPES
    if content_type not in allowed:
        message = (
            "Documents must be PDF, JPEG, PNG, WebP, or GIF"
            if kind == "document"
            else "Pictures must be JPEG, PNG, WebP, or GIF"
        )
        raise HTTPException(status_code=400, detail=message)
    key = _public_key(body.get("key"))
    url = r2.presign_put_key(key, content_type)
    return {
        "upload_url": url,
        "url": url,
        "object_key": key,
        "key": key,
        "content_type": content_type,
        "headers": {"content-type": content_type},
        "method": "PUT",
    }


@router.post("/scraps", status_code=status.HTTP_201_CREATED)
def create_scrap(request: Request, body: dict = Body(...)) -> dict:
    _require_admin(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    scrap_type = body.get("type")
    if (
        not isinstance(scrap_type, str)
        or not TYPE_PATTERN.match(scrap_type.strip())
        or not 2 <= len(scrap_type.strip()) <= 40
    ):
        raise HTTPException(status_code=400, detail="Scrap type must be a short name such as bikes")
    price = body.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)) or price < 0 or price > 100_000_000:
        raise HTTPException(status_code=400, detail="Price must be a number from 0 up")
    pictures = _files(body.get("pictures") or [], "pictures", 3)
    documents = _files(body.get("documents") or [], "documents", 5)
    if not pictures and not documents:
        raise HTTPException(status_code=400, detail="Add scrap pictures or documents")
    scrap = {
        "id": uuid.uuid4().hex,
        "type": scrap_type.strip(),
        "price": round(float(price), 2),
        "currency": "INR",
        "portal": SLAVE,
        "pictures": pictures,
        "documents": documents,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    scraps = _load()
    scraps.insert(0, scrap)
    _save(scraps)
    return {"scrap": scrap}


@router.delete("/scraps/{scrap_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def delete_scrap(scrap_id: str, request: Request) -> Response:
    _require_admin(request)
    scraps = _load()
    found = next((scrap for scrap in scraps if scrap.get("id") == scrap_id), None)
    if found is None:
        raise HTTPException(status_code=404, detail="Scrap not found")
    for group in ("pictures", "documents"):
        for item in found.get(group) or []:
            if isinstance(item, dict) and isinstance(item.get("object_key"), str):
                r2.delete_key(item["object_key"])
    _save([scrap for scrap in scraps if scrap.get("id") != scrap_id])
    return Response(status_code=status.HTTP_204_NO_CONTENT)
