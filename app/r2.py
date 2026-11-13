"""Cloudflare R2 via the S3 API.

Same shape as junctionBack `app/r2_media.py`: boto3 presigned PUT, immutable
object keys, public URL built from the CDN base. Keys live under `herb/`
so this bucket stays separate from Junction shorts.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException, status

MediaKind = Literal["image", "document", "audio", "video"]
MediaPlace = Literal["schedule", "coach", "centre"]

# Video types and the 40 MB cap match junctionBack shorts (`video/mp4`, `video/webm`,
# `video/quicktime`). Keys stay under `herb/` so they never land in `shorts/`.
_KIND_EXTS: dict[MediaKind, dict[str, str]] = {
    "image": {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    },
    "document": {
        "application/pdf": ".pdf",
    },
    "audio": {
        "audio/mpeg": ".mp3",
        "audio/mp4": ".m4a",
        "audio/aac": ".aac",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/webm": ".webm",
    },
    "video": {
        "video/mp4": ".mp4",
        "video/webm": ".webm",
        "video/quicktime": ".mov",
    },
}

_CONTENT_TYPE_ALIASES = {
    "image/jpg": "image/jpeg",
    "audio/mp3": "audio/mpeg",
    "audio/x-m4a": "audio/mp4",
    "application/x-pdf": "application/pdf",
    "video/x-matroska": "video/webm",
    "video/mkv": "video/webm",
    "application/mp4": "video/mp4",
}

_PREFIX = {
    "image": "herb/images",
    "document": "herb/documents",
    "audio": "herb/audio",
    "video": "herb/videos",
}

_KEY_RE = re.compile(
    r"^herb/(images|documents|audio|videos)"
    r"(?:/(?:schedule|coach|centre))?"
    r"/[a-f0-9]{32}\.(jpg|png|webp|gif|pdf|mp3|m4a|wav|aac|webm|mp4|mov)$"
)

MAX_BYTES: dict[MediaKind, int] = {
    "image": 8 * 1024 * 1024,
    "document": 15 * 1024 * 1024,
    "audio": 20 * 1024 * 1024,
    "video": 40 * 1024 * 1024,
}


@dataclass(frozen=True)
class R2Settings:
    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base_url: str
    endpoint: str
    sign_expires_seconds: int
    cache_control: str

    @property
    def configured(self) -> bool:
        return bool(
            self.access_key_id
            and self.secret_access_key
            and self.bucket
            and self.public_base_url
            and self.endpoint
        )


def _clean(name: str) -> str:
    return (os.getenv(name) or "").strip()


def load_settings() -> R2Settings:
    account_id = _clean("R2_ACCOUNT_ID")
    endpoint = _clean("R2_ENDPOINT") or (
        f"https://{account_id}.r2.cloudflarestorage.com" if account_id else ""
    )
    try:
        expires = int(_clean("R2_SIGN_EXPIRES_SECONDS") or "600")
    except ValueError:
        expires = 600
    expires = min(max(expires, 60), 3600)
    return R2Settings(
        account_id=account_id,
        access_key_id=_clean("R2_ACCESS_KEY_ID"),
        secret_access_key=_clean("R2_SECRET_ACCESS_KEY"),
        bucket=_clean("R2_BUCKET"),
        public_base_url=_clean("R2_PUBLIC_BASE_URL").rstrip("/"),
        endpoint=endpoint,
        sign_expires_seconds=expires,
        cache_control=_clean("R2_CACHE_CONTROL") or "public, max-age=31536000, immutable",
    )


def r2_missing_env() -> list[str]:
    settings = load_settings()
    missing: list[str] = []
    if not settings.account_id and not _clean("R2_ENDPOINT"):
        missing.append("R2_ACCOUNT_ID")
    if not settings.access_key_id:
        missing.append("R2_ACCESS_KEY_ID")
    if not settings.secret_access_key:
        missing.append("R2_SECRET_ACCESS_KEY")
    if not settings.bucket:
        missing.append("R2_BUCKET")
    if not settings.public_base_url:
        missing.append("R2_PUBLIC_BASE_URL")
    if not settings.endpoint:
        missing.append("R2_ENDPOINT")
    return missing


def r2_configured() -> bool:
    return load_settings().configured


def require_r2() -> R2Settings:
    settings = load_settings()
    if not settings.configured:
        missing = ", ".join(r2_missing_env()) or "R2_*"
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"R2 storage is not configured (missing {missing})",
        )
    return settings


@lru_cache(maxsize=4)
def _client(endpoint: str, access_key_id: str, secret_access_key: str):
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def client_for(settings: R2Settings):
    return _client(settings.endpoint, settings.access_key_id, settings.secret_access_key)


def normalize_content_type(kind: MediaKind, content_type: str, filename: str = "") -> str:
    raw = (content_type or "").split(";")[0].strip().lower()
    raw = _CONTENT_TYPE_ALIASES.get(raw, raw)
    if raw in _KIND_EXTS[kind]:
        return raw
    name = (filename or "").strip().lower()
    guesses: list[tuple[str, str]] = []
    if kind == "image":
        guesses = [
            (".png", "image/png"),
            (".webp", "image/webp"),
            (".gif", "image/gif"),
            (".jpg", "image/jpeg"),
            (".jpeg", "image/jpeg"),
        ]
    elif kind == "document":
        guesses = [(".pdf", "application/pdf")]
    elif kind == "video":
        guesses = [
            (".webm", "video/webm"),
            (".mov", "video/quicktime"),
            (".m4v", "video/mp4"),
            (".mp4", "video/mp4"),
        ]
    else:
        guesses = [
            (".mp3", "audio/mpeg"),
            (".m4a", "audio/mp4"),
            (".aac", "audio/aac"),
            (".wav", "audio/wav"),
            (".webm", "audio/webm"),
        ]
    for suffix, mime in guesses:
        if name.endswith(suffix):
            return mime
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=f"Unsupported {kind} content type",
    )


def public_url(object_key: str) -> str:
    settings = require_r2()
    return f"{settings.public_base_url}/{object_key.lstrip('/')}"


def is_valid_object_key(object_key: str) -> bool:
    return bool(_KEY_RE.match((object_key or "").strip()))


def assert_owned_key(object_key: str) -> str:
    key = (object_key or "").strip()
    if not is_valid_object_key(key):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid object key")
    return key


def new_object_key(kind: MediaKind, content_type: str, place: MediaPlace | None = None) -> str:
    ext = _KIND_EXTS[kind].get(content_type)
    if not ext:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported {kind} content type",
        )
    folder = f"{_PREFIX[kind]}/{place}" if place else _PREFIX[kind]
    return f"{folder}/{uuid.uuid4().hex}{ext}"


def presign_put(
    *,
    kind: MediaKind,
    content_type: str,
    filename: str = "",
    place: MediaPlace | None = None,
) -> dict:
    settings = require_r2()
    content_type = normalize_content_type(kind, content_type, filename)
    object_key = new_object_key(kind, content_type, place)
    upload_url = client_for(settings).generate_presigned_url(
        "put_object",
        Params={
            "Bucket": settings.bucket,
            "Key": object_key,
            "ContentType": content_type,
        },
        ExpiresIn=settings.sign_expires_seconds,
    )
    return {
        "object_key": object_key,
        "upload_url": upload_url,
        "public_url": f"{settings.public_base_url}/{object_key}",
        "content_type": content_type,
        "expires_in": settings.sign_expires_seconds,
        "kind": kind,
        "upload_method": "PUT",
        "max_bytes": MAX_BYTES[kind],
        "headers": {"Content-Type": content_type},
    }


def _raise_storage_error(exc: Exception) -> None:
    raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="R2 request failed") from exc


def put_bytes(object_key: str, body: bytes, *, content_type: str) -> None:
    settings = require_r2()
    key = assert_owned_key(object_key)
    try:
        client_for(settings).put_object(
            Bucket=settings.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl=settings.cache_control,
        )
    except (BotoCoreError, ClientError) as exc:
        _raise_storage_error(exc)


def delete_object(object_key: str) -> None:
    settings = require_r2()
    key = assert_owned_key(object_key)
    delete_key(key)


def read_object(key: str) -> bytes | None:
    """Read a bucket key. A missing object is None. Used by the scrap catalog."""
    settings = require_r2()
    try:
        response = client_for(settings).get_object(Bucket=settings.bucket, Key=key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if http_status == 404 or code in {"NoSuchKey", "404", "NotFound"}:
            return None
        _raise_storage_error(exc)
    return response["Body"].read()


def write_object(key: str, body: bytes, *, content_type: str, cache_control: str | None = None) -> None:
    settings = require_r2()
    try:
        client_for(settings).put_object(
            Bucket=settings.bucket,
            Key=key,
            Body=body,
            ContentType=content_type,
            CacheControl=cache_control or settings.cache_control,
        )
    except (BotoCoreError, ClientError) as exc:
        _raise_storage_error(exc)


def presign_put_key(key: str, content_type: str) -> str:
    settings = require_r2()
    return client_for(settings).generate_presigned_url(
        "put_object",
        Params={"Bucket": settings.bucket, "Key": key, "ContentType": content_type},
        ExpiresIn=settings.sign_expires_seconds,
    )


def delete_key(key: str) -> None:
    settings = require_r2()
    try:
        client_for(settings).delete_object(Bucket=settings.bucket, Key=key)
    except (BotoCoreError, ClientError) as exc:
        _raise_storage_error(exc)
