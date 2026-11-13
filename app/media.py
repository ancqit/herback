"""Upload, presign, and delete for herbant files in R2."""

from __future__ import annotations

import hmac
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Response, UploadFile, status
from pydantic import BaseModel, Field

from .r2 import MAX_BYTES, MediaKind, delete_object, new_object_key, normalize_content_type, presign_put, put_bytes, require_r2

router = APIRouter(prefix="/media", tags=["media"])


class SignRequest(BaseModel):
    kind: MediaKind
    content_type: str = ""
    filename: str = Field(default="", max_length=200)


class SignResponse(BaseModel):
    kind: MediaKind
    object_key: str
    upload_url: str
    public_url: str
    content_type: str
    expires_in: int
    upload_method: Literal["PUT"] = "PUT"
    max_bytes: int
    headers: dict[str, str]


class StoredObject(BaseModel):
    kind: MediaKind
    object_key: str
    public_url: str
    content_type: str
    size_bytes: int


class DeleteRequest(BaseModel):
    object_key: str = Field(min_length=1, max_length=200)


def require_api_key(
    x_herb_key: Annotated[str | None, Header()] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    expected = (os.getenv("HERB_API_KEY") or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="HERB_API_KEY is not set",
        )
    provided = (x_herb_key or "").strip()
    if not provided and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer":
            provided = token.strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")


@router.post("/sign", response_model=SignResponse)
def sign_upload(payload: SignRequest, _: None = Depends(require_api_key)) -> SignResponse:
    """Mint a short-lived PUT URL. The browser uploads straight to R2.

    The PUT must send the returned Content-Type header. That header is part
    of the signature. The bucket itself also needs a CORS rule for the
    herbant origin, or the browser will block the PUT.
    """
    signed = presign_put(kind=payload.kind, content_type=payload.content_type, filename=payload.filename)
    return SignResponse(**signed)


@router.post("", response_model=StoredObject, status_code=status.HTTP_201_CREATED)
async def upload_object(
    _: None = Depends(require_api_key),
    kind: MediaKind = Form(...),
    file: UploadFile = File(...),
) -> StoredObject:
    """Server-side PUT for small files. Prefer /media/sign so Render does not proxy the bytes."""
    settings = require_r2()
    content_type = normalize_content_type(kind, file.content_type or "", file.filename or "")
    limit = MAX_BYTES[kind]
    chunks: list[bytes] = []
    total = 0
    while True:
        block = await file.read(1024 * 1024)
        if not block:
            break
        total += len(block)
        if total > limit:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"File must be {limit // (1024 * 1024)} MB or smaller",
            )
        chunks.append(block)
    body = b"".join(chunks)
    if not body:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty")
    object_key = new_object_key(kind, content_type)
    put_bytes(object_key, body, content_type=content_type)
    return StoredObject(
        kind=kind,
        object_key=object_key,
        public_url=f"{settings.public_base_url}/{object_key}",
        content_type=content_type,
        size_bytes=len(body),
    )


@router.delete("", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
def remove_object(payload: DeleteRequest, _: None = Depends(require_api_key)) -> Response:
    delete_object(payload.object_key)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
