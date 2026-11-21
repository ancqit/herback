"""Read a stored picture, clip, or paper back from the bucket.

The portals upload into the scrap bucket. The public host on that bucket does
not serve the file, so a page that uses it draws a broken picture. These URLs
are what the page loads. Mongo still keeps the CDN address on the form.
"""

from __future__ import annotations

import os
import re

from fastapi import APIRouter, HTTPException, Response, status

from . import r2

router = APIRouter(tags=["files"])

_HEX = r"[a-f0-9]{32}"
_SERVEABLE = re.compile(
    rf"^(?:herb/(?:images|documents|audio|videos)(?:/(?:schedule|coach|centre))?/{_HEX}\.(?:jpg|png|webp|gif|pdf|mp3|m4a|wav|aac|webm|mp4|mov)"
    rf"|cart/photos/{_HEX}\.(?:jpg|png|webp)"
    rf"|binter/photos/{_HEX}\.(?:jpg|png|webp)"
    rf"|binter/clips/{_HEX}\.(?:mp4|webm|mov)"
    rf"|binter/documents/(?:aadhaar|licence|rc)/{_HEX}\.(?:jpg|png|webp|pdf)"
    rf"|2001(?:/[A-Za-z0-9][A-Za-z0-9._-]{{0,80}})+)$"
)
_MEDIA = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "gif": "image/gif",
    "pdf": "application/pdf",
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "wav": "audio/wav",
    "aac": "audio/aac",
    "mp4": "video/mp4",
    "mov": "video/quicktime",
}


def service_origin() -> str:
    raw = (os.getenv("HERBACK_PUBLIC_URL") or "https://herback-1.onrender.com").strip().rstrip("/")
    if raw.startswith("http://"):
        raw = "https://" + raw[len("http://") :]
    return raw


def serveable(key: str) -> bool:
    """A portal file. Catalogs and cart state stay off this route."""
    if not isinstance(key, str):
        return False
    cleaned = key.strip().lstrip("/")
    if cleaned == "2001/catalog.json" or cleaned.endswith("/catalog.json"):
        return False
    return bool(_SERVEABLE.fullmatch(cleaned))


def file_url(key: str) -> str:
    return f"{service_origin()}/files/{key.strip().lstrip('/')}"


def media_type_for(key: str) -> str:
    ext = key.rsplit(".", 1)[-1].lower()
    if ext == "webm":
        if "/audio/" in key:
            return "audio/webm"
        return "video/webm"
    return _MEDIA.get(ext, "application/octet-stream")


def serve_key(key: str) -> Response:
    cleaned = key.strip().lstrip("/")
    if not serveable(cleaned):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That file is not in the bucket.")
    body = r2.read_object(cleaned)
    if not body:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That file is not in the bucket.")
    return Response(
        content=body,
        media_type=media_type_for(cleaned),
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": "inline",
        },
    )


@router.get("/files/{key:path}")
def read_file(key: str) -> Response:
    """Return one stored file. The key is the object name, not a public host."""
    return serve_key(key)
