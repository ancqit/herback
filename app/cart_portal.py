"""Vegetable carts.

Fresh photographs live in R2 under ``cart/photos/``. The cart form lives in
Mongo and keeps each photograph's CDN URL. herbant stays under ``herb/``,
scrap under ``2001/``, and binter under ``binter/``. ``CART_ENTITY_TOKEN`` and
``CART_LOADER_TOKEN`` are the two doors. A blank value keeps that door shut.
A cart token is the cart. A driver token is issued when vegetables or other
things are chosen for one of the cart's addresses. Each address has its own
schedule.
"""

from __future__ import annotations

import hmac
import os
import re
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Body, HTTPException, Request, Response, status

from . import forms, r2

router = APIRouter(prefix="/cart", tags=["cart"])

STATE_KEY = "cart/state.json"
RETENTION = timedelta(days=30)
PHOTO_MAX_BYTES = 8 * 1024 * 1024
MAX_CARTS = 40
MAX_ITEMS = 30
MAX_STOPS = 12
_LOCK = threading.Lock()

DOOR_SHUT = "That number does not open the portal."
WRONG_ROLE = "That number cannot do this."
NOT_FOUND = "That cart is not in the portal."
NOT_LOADED = "That cart is not loaded."
BAD_ITEM = "Add something from this cart."
BAD_KIND = "Choose a vegetable or another thing."
BAD_ADDRESS = "Address must be 8 to 160 characters."
NEED_STOP = "Choose one of this cart's addresses."
LAST_STOP = "A cart keeps at least one address."
TOO_MANY_STOPS = "This cart already has 12 addresses."
BAD_TOKEN = "Cart token must be 4 to 32 letters or numbers."
TAKEN = "That cart token is already in use."
BAD_PRICE = "Price must be a number from 0 to 10000."
BAD_UNIT = "Choose kg, bunch, or piece."
BAD_PHOTO = "A fresh photograph is required."
BAD_IMAGE = "A photograph must be an image."
BAD_DAY = "Choose at least one day."
BAD_TIME = "The round needs a start and an end."
BAD_END = "The round has to end after it starts."
BAD_NAME = "Name must be 2 to 40 characters."
BAD_NOTE = "Note must be 80 characters or fewer."
BAD_QTY = "Quantity must be from 1 to 20."
FULL = "This cart is full."
TOO_MANY = "The portal already holds 40 carts."
NO_DRIVER = "That token is not a driver token."
INVALID_KEY = "invalid key"

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
UNITS = ("kg", "bunch", "piece")
KINDS = ("veggie", "other")
PHOTO_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
_PHOTO_MEDIA = {"jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
_ALIASES = {"image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg"}
_HEX = r"[a-f0-9]{32}"
_PHOTO_KEY = re.compile(rf"^cart/photos/{_HEX}\.(jpg|png|webp)$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{4,32}$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_NAME_MARKS = " .'’-"
_NOTE_MARKS = " .'’,-"
_DRIVER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_FOREIGN_PREFIXES = ("herb/", "2001/", "shorts/", "binter/")


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


def _env_role(token: str) -> str | None:
    if _same(token, _env_token("CART_LOADER_TOKEN")):
        return "loader"
    if _same(token, _env_token("CART_ENTITY_TOKEN")):
        return "entity"
    return None


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization") or ""
    match = re.fullmatch(r"Bearer (\S+)", header.strip())
    return match.group(1) if match else ""


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _short_name(raw: object) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail=BAD_NAME)
    name = _collapse(raw)
    if not 2 <= len(name) <= 40 or not all(ch.isalnum() or ch in _NAME_MARKS for ch in name):
        raise HTTPException(status_code=400, detail=BAD_NAME)
    return name


def _address(raw: object) -> str:
    if not isinstance(raw, str):
        raise HTTPException(status_code=400, detail=BAD_ADDRESS)
    label = _collapse(raw)
    if not 8 <= len(label) <= 160 or any(ord(ch) < 32 for ch in label):
        raise HTTPException(status_code=400, detail=BAD_ADDRESS)
    return label


def _schedule(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail=BAD_DAY)
    days_raw = raw.get("days")
    if not isinstance(days_raw, list):
        raise HTTPException(status_code=400, detail=BAD_DAY)
    days = [day for day in DAYS if day in days_raw]
    if not days:
        raise HTTPException(status_code=400, detail=BAD_DAY)
    start = raw.get("start")
    end = raw.get("end")
    if not isinstance(start, str) or not _TIME_RE.fullmatch(start):
        raise HTTPException(status_code=400, detail=BAD_TIME)
    if not isinstance(end, str) or not _TIME_RE.fullmatch(end):
        raise HTTPException(status_code=400, detail=BAD_TIME)
    if start >= end:
        raise HTTPException(status_code=400, detail=BAD_END)
    note_raw = raw.get("note") if raw.get("note") is not None else ""
    if not isinstance(note_raw, str):
        raise HTTPException(status_code=400, detail=BAD_NOTE)
    note = _collapse(note_raw)
    if len(note) > 80 or (note and not all(ch.isalnum() or ch in _NOTE_MARKS for ch in note)):
        raise HTTPException(status_code=400, detail=BAD_NOTE)
    return {"days": days, "start": start, "end": end, "note": note}


def _kind(raw: object) -> str:
    if raw not in KINDS:
        raise HTTPException(status_code=400, detail=BAD_KIND)
    return str(raw)


def _stop(raw: object, *, stop_id: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail=BAD_ADDRESS)
    return {
        "id": stop_id or uuid.uuid4().hex,
        "address": _address(raw.get("address")),
        "schedule": _schedule(raw.get("schedule")),
    }


def _stops(raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail=BAD_ADDRESS)
    if len(raw) > MAX_STOPS:
        raise HTTPException(status_code=400, detail=TOO_MANY_STOPS)
    return [_stop(entry) for entry in raw]


def _price(raw: object) -> float:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw < 0 or raw > 10000:
        raise HTTPException(status_code=400, detail=BAD_PRICE)
    return round(float(raw), 2)


def _unit(raw: object) -> str:
    if raw not in UNITS:
        raise HTTPException(status_code=400, detail=BAD_UNIT)
    return str(raw)


def _cart_token(raw: object) -> str:
    if not isinstance(raw, str) or not _TOKEN_RE.fullmatch(raw.strip()):
        raise HTTPException(status_code=400, detail=BAD_TOKEN)
    return raw.strip()


def _normalize_type(raw: object) -> str:
    text = str(raw or "").split(";")[0].strip().lower()
    return _ALIASES.get(text, text)


def _content_type(raw: object) -> str:
    content_type = _normalize_type(raw)
    if content_type not in PHOTO_TYPES:
        raise HTTPException(status_code=400, detail=BAD_IMAGE)
    return content_type


def _filename(raw: object, fallback: str) -> str:
    if not isinstance(raw, str):
        return fallback
    name = _collapse(raw.replace("\\", "/").split("/")[-1])
    if name in {"", ".", ".."} or "://" in name or name.lower().startswith("http"):
        return fallback
    return name[:80]


def _mint_photo_key(content_type: str) -> str:
    return f"cart/photos/{uuid.uuid4().hex}{PHOTO_TYPES[content_type]}"


def _photo(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail=BAD_PHOTO)
    content_type = _content_type(raw.get("content_type") or raw.get("contentType") or "")
    key = raw.get("object_key")
    if key is None:
        key = raw.get("objectKey")
    if key is None:
        key = raw.get("key")
    if not isinstance(key, str):
        raise HTTPException(status_code=400, detail=INVALID_KEY)
    key = key.strip()
    if key.startswith(_FOREIGN_PREFIXES) or not _PHOTO_KEY.fullmatch(key):
        raise HTTPException(status_code=400, detail=INVALID_KEY)
    if not key.endswith(PHOTO_TYPES[content_type]):
        raise HTTPException(status_code=400, detail=INVALID_KEY)
    return forms.remember_cdn(
        {
            "object_key": key,
            "content_type": content_type,
            "filename": _filename(raw.get("filename"), f"photo{PHOTO_TYPES[content_type]}"),
        },
        key,
    )


def _empty_state() -> dict:
    return {"carts": [], "orders": []}


def _read_state() -> dict:
    return forms.adopt_bucket("cart", STATE_KEY, _empty_state(), forms.cart_form)


def _write_state(state: dict) -> None:
    forms.save_form("cart", {"carts": state["carts"], "orders": state["orders"]})


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


def _sweep(state: dict, now: datetime) -> bool:
    kept = [order for order in state["orders"] if not _expired(order.get("created_at"), now)]
    dropped = len(kept) != len(state["orders"])
    state["orders"] = kept
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


def _lookup(token: str, state: dict) -> tuple[str, str] | None:
    for cart in state["carts"]:
        if isinstance(cart.get("token"), str) and isinstance(cart.get("id"), str) and _same(token, cart["token"]):
            return "cart", cart["id"]
    for order in state["orders"]:
        if (
            isinstance(order.get("driver_token"), str)
            and isinstance(order.get("id"), str)
            and _same(token, order["driver_token"])
        ):
            return "driver", order["id"]
    return None


def _taken(token: str, state: dict) -> bool:
    if _env_role(token):
        return True
    return _lookup(token, state) is not None


def _open(request: Request) -> tuple[str, str | None]:
    token = _bearer(request)
    role = _env_role(token)
    if role:
        return role, None

    def read(state: dict) -> tuple[str, str | None]:
        found = _lookup(token, state)
        if found is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DOOR_SHUT)
        return found

    return _with_state(read, save=False)


def _allow(request: Request, *roles: str) -> tuple[str, str | None]:
    role, extra = _open(request)
    if role not in roles:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)
    return role, extra


def _stock(request: Request, cart_id: str) -> None:
    """The loader, the entity, or this cart's own token may put things on it."""
    role, own_id = _allow(request, "loader", "entity", "cart")
    if role == "cart" and own_id != cart_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)


def _find_cart(state: dict, cart_id: str) -> dict:
    for cart in state["carts"]:
        if cart.get("id") == cart_id:
            return cart
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)


def _find_order(state: dict, order_id: str) -> dict:
    for order in state["orders"]:
        if order.get("id") == order_id:
            return order
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NO_DRIVER)


def _photo_host() -> str:
    raw = (os.getenv("HERBACK_PUBLIC_URL") or "https://herback-1.onrender.com").strip().rstrip("/")
    if raw.startswith("http://"):
        raw = "https://" + raw[len("http://") :]
    return raw


def _display_photo_url(photo: dict) -> str:
    """The address the cart page puts on the image.

    The file stays in the scrap bucket. The public bucket host is not what the
    page loads: that host was still the old one, so the picture drew broken.
    This address is read back from the bucket with an image content type.
    """
    key = photo.get("object_key")
    if isinstance(key, str) and _PHOTO_KEY.fullmatch(key):
        return f"{_photo_host()}/cart/photos/{key.rsplit('/', 1)[-1]}"
    return forms.playback_url(photo)


def _public_photo(photo: dict) -> dict:
    return {
        "url": _display_photo_url(photo),
        "content_type": str(photo.get("content_type") or ""),
        "filename": str(photo.get("filename") or ""),
    }


def _stored_items(cart: dict) -> list[dict]:
    if isinstance(cart.get("items"), list):
        return [item for item in cart["items"] if isinstance(item, dict)]
    veggies = cart.get("veggies") if isinstance(cart.get("veggies"), list) else []
    rows: list[dict] = []
    for item in veggies:
        if isinstance(item, dict):
            copied = dict(item)
            copied.setdefault("kind", "veggie")
            rows.append(copied)
    return rows


def _stored_stops(cart: dict) -> list[dict]:
    if isinstance(cart.get("stops"), list) and cart["stops"]:
        return [stop for stop in cart["stops"] if isinstance(stop, dict)]
    schedule = cart.get("schedule")
    if isinstance(schedule, dict):
        address = cart.get("address") if isinstance(cart.get("address"), str) else ""
        return [{"id": cart.get("id"), "address": address, "schedule": schedule}]
    return []


def _adopt(cart: dict) -> None:
    cart["items"] = _stored_items(cart)
    cart["stops"] = _stored_stops(cart)
    cart.pop("veggies", None)
    cart.pop("schedule", None)


def _public_item(item: dict) -> dict:
    photo = item.get("photo") if isinstance(item.get("photo"), dict) else {}
    return {
        "id": item.get("id"),
        "kind": item.get("kind") if item.get("kind") in KINDS else "veggie",
        "name": item.get("name"),
        "price": item.get("price"),
        "unit": item.get("unit"),
        "photo": _public_photo(photo),
    }


def _public_stop(stop: dict) -> dict:
    return {
        "id": stop.get("id"),
        "address": stop.get("address"),
        "schedule": stop.get("schedule"),
    }


def _public_cart(cart: dict, *, show_token: bool) -> dict:
    items = _stored_items(cart)
    body = {
        "id": cart.get("id"),
        "name": cart.get("name"),
        "stops": [_public_stop(stop) for stop in _stored_stops(cart)],
        "items": [_public_item(item) for item in items],
        "loaded": bool(items),
        "created_at": cart.get("created_at"),
    }
    if show_token:
        body["token"] = cart.get("token")
    return body


def _visible(cart: dict, role: str, cart_id: str | None) -> bool:
    if role == "loader":
        return True
    if role == "cart":
        return cart.get("id") == cart_id
    if role == "entity":
        return bool(_stored_items(cart))
    return False


def _driver_token(state: dict) -> str:
    for _ in range(40):
        candidate = "".join(secrets.choice(_DRIVER_ALPHABET) for _ in range(8))
        if not _taken(candidate, state):
            return candidate
    return uuid.uuid4().hex[:8].upper()


def _lines(cart: dict, raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ITEMS:
        raise HTTPException(status_code=400, detail=BAD_ITEM)
    items = {
        item.get("id"): item
        for item in _stored_items(cart)
        if isinstance(item.get("id"), str)
    }
    if not items:
        raise HTTPException(status_code=400, detail=NOT_LOADED)
    merged: dict[str, int] = {}
    order: list[str] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise HTTPException(status_code=400, detail=BAD_ITEM)
        item_id = entry.get("item_id")
        qty = entry.get("qty")
        if not isinstance(item_id, str) or item_id not in items:
            raise HTTPException(status_code=400, detail=BAD_ITEM)
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 20:
            raise HTTPException(status_code=400, detail=BAD_QTY)
        if item_id not in merged:
            order.append(item_id)
            merged[item_id] = 0
        merged[item_id] += qty
        if merged[item_id] > 20:
            raise HTTPException(status_code=400, detail=BAD_QTY)
    lines = []
    for item_id in order:
        item = items[item_id]
        lines.append(
            {
                "item_id": item_id,
                "kind": item.get("kind") if item.get("kind") in KINDS else "veggie",
                "name": item.get("name"),
                "price": item.get("price"),
                "unit": item.get("unit"),
                "qty": merged[item_id],
            }
        )
    return lines


def _public_slip(order: dict) -> dict:
    lines = order.get("lines") if isinstance(order.get("lines"), list) else []
    total = round(sum(float(line.get("price") or 0) * int(line.get("qty") or 0) for line in lines if isinstance(line, dict)), 2)
    return {
        "id": order.get("id"),
        "driver_token": order.get("driver_token"),
        "cart_id": order.get("cart_id"),
        "cart_name": order.get("cart_name"),
        "schedule": order.get("schedule"),
        "address": order.get("address"),
        "lines": lines,
        "total": total,
        "status": order.get("status"),
        "created_at": order.get("created_at"),
    }


def _photo_keys(cart: dict) -> list[str]:
    keys: list[str] = []
    for item in _stored_items(cart):
        photo = item.get("photo")
        if isinstance(photo, dict) and isinstance(photo.get("object_key"), str):
            key = photo["object_key"]
            if key.startswith("cart/photos/"):
                keys.append(key)
    return keys


@router.post("/login")
def login(body: dict = Body(...)) -> dict:
    token = body.get("token") if isinstance(body, dict) else None
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DOOR_SHUT)
    role = _env_role(token)
    if role:
        return {"ok": True, "role": role}

    def read(state: dict) -> dict:
        found = _lookup(token, state)
        if found is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=DOOR_SHUT)
        role_name, extra = found
        payload: dict[str, str] = {"ok": True, "role": role_name}
        if role_name == "cart":
            payload["cart_id"] = extra
        return payload

    return _with_state(read, save=False)


@router.get("/photos/{name}")
def photograph(name: str) -> Response:
    """Return one cart photograph. The name is the object file, not a path."""
    key = f"cart/photos/{name}"
    if not _PHOTO_KEY.fullmatch(key):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That photograph is not in the bucket.")
    body = r2.read_object(key)
    if not body:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="That photograph is not in the bucket.")
    media = _PHOTO_MEDIA[key.rsplit(".", 1)[-1]]
    return Response(
        content=body,
        media_type=media,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "Content-Disposition": "inline",
        },
    )


@router.post("/upload-url")
def upload_url(request: Request, body: dict = Body(...)) -> dict:
    _allow(request, "loader", "entity", "cart")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    content_type = _content_type(body.get("contentType") or body.get("content_type") or "")
    r2.require_r2()
    key = _mint_photo_key(content_type)
    upload = r2.presign_put_key(key, content_type)
    return {
        "upload_url": upload,
        "cdn": r2.public_url(key),
        "object_key": key,
        "content_type": content_type,
        "headers": {"content-type": content_type},
        "method": "PUT",
        "max_bytes": PHOTO_MAX_BYTES,
        "max_seconds": None,
    }


@router.get("/carts")
def list_carts(request: Request) -> dict:
    role, cart_id = _allow(request, "entity", "loader", "cart")

    def read(state: dict) -> dict:
        show = role == "loader"
        carts = [
            _public_cart(cart, show_token=show)
            for cart in state["carts"]
            if _visible(cart, role, cart_id)
        ]
        return {"carts": carts}

    return _with_state(read, save=False)


@router.post("/carts", status_code=status.HTTP_201_CREATED)
def create_cart(request: Request, body: dict = Body(...)) -> dict:
    _allow(request, "loader")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    token = _cart_token(body.get("token"))
    name = _short_name(body.get("name"))
    stops = _stops(body.get("stops"))

    def write(state: dict) -> dict:
        if len(state["carts"]) >= MAX_CARTS:
            raise HTTPException(status_code=400, detail=TOO_MANY)
        if _taken(token, state):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=TAKEN)
        cart = {
            "id": uuid.uuid4().hex,
            "token": token,
            "name": name,
            "stops": stops,
            "items": [],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["carts"].insert(0, cart)
        return {"cart": _public_cart(cart, show_token=True)}

    return _with_state(write, save=True)


@router.get("/carts/{cart_id}")
def get_cart(cart_id: str, request: Request) -> dict:
    role, own_id = _allow(request, "entity", "loader", "cart")
    if role == "cart" and own_id != cart_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)

    def read(state: dict) -> dict:
        return {"cart": _public_cart(_find_cart(state, cart_id), show_token=role == "loader")}

    return _with_state(read, save=False)


@router.post("/carts/{cart_id}/stops", status_code=status.HTTP_201_CREATED)
def add_stop(cart_id: str, request: Request, body: dict = Body(...)) -> dict:
    _allow(request, "loader")
    stop = _stop(body)

    def write(state: dict) -> dict:
        cart = _find_cart(state, cart_id)
        _adopt(cart)
        if len(cart["stops"]) >= MAX_STOPS:
            raise HTTPException(status_code=400, detail=TOO_MANY_STOPS)
        cart["stops"].append(stop)
        return {"cart": _public_cart(cart, show_token=True)}

    return _with_state(write, save=True)


@router.delete("/carts/{cart_id}/stops/{stop_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_stop(cart_id: str, stop_id: str, request: Request) -> Response:
    _allow(request, "loader")

    def write(state: dict) -> None:
        cart = _find_cart(state, cart_id)
        _adopt(cart)
        stops = cart["stops"]
        if not any(stop.get("id") == stop_id for stop in stops):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NEED_STOP)
        if len(stops) <= 1:
            raise HTTPException(status_code=400, detail=LAST_STOP)
        cart["stops"] = [stop for stop in stops if stop.get("id") != stop_id]

    _with_state(write, save=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/carts/{cart_id}/items", status_code=status.HTTP_201_CREATED)
def add_item(cart_id: str, request: Request, body: dict = Body(...)) -> dict:
    _stock(request, cart_id)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    kind = _kind(body.get("kind"))
    name = _short_name(body.get("name"))
    price = _price(body.get("price"))
    unit = _unit(body.get("unit"))
    if body.get("photo") is None:
        raise HTTPException(status_code=400, detail=BAD_PHOTO)
    photo = _photo(body.get("photo"))

    def write(state: dict) -> dict:
        cart = _find_cart(state, cart_id)
        _adopt(cart)
        if len(cart["items"]) >= MAX_ITEMS:
            raise HTTPException(status_code=400, detail=FULL)
        cart["items"].append(
            {
                "id": uuid.uuid4().hex,
                "kind": kind,
                "name": name,
                "price": price,
                "unit": unit,
                "photo": photo,
            }
        )
        return {"cart": _public_cart(cart, show_token=True)}

    return _with_state(write, save=True)


@router.delete("/carts/{cart_id}/items/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_item(cart_id: str, item_id: str, request: Request) -> Response:
    _stock(request, cart_id)

    def write(state: dict) -> None:
        cart = _find_cart(state, cart_id)
        _adopt(cart)
        items = cart["items"]
        found = next((item for item in items if item.get("id") == item_id), None)
        if found is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=BAD_ITEM)
        photo = found.get("photo") if isinstance(found.get("photo"), dict) else {}
        key = photo.get("object_key")
        if isinstance(key, str) and key.startswith("cart/photos/"):
            r2.delete_key(key)
        cart["items"] = [item for item in items if item.get("id") != item_id]

    _with_state(write, save=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete("/carts/{cart_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_cart(cart_id: str, request: Request) -> Response:
    _allow(request, "loader")

    def write(state: dict) -> None:
        cart = _find_cart(state, cart_id)
        for key in _photo_keys(cart):
            r2.delete_key(key)
        state["carts"] = [item for item in state["carts"] if item.get("id") != cart_id]

    _with_state(write, save=True)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/orders", status_code=status.HTTP_201_CREATED)
def place_order(request: Request, body: dict = Body(...)) -> dict:
    role, own_id = _allow(request, "entity", "cart")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected JSON object")
    cart_id = body.get("cart_id")
    if not isinstance(cart_id, str) or not cart_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=NOT_FOUND)
    if role == "cart" and own_id != cart_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)
    stop_id = body.get("stop_id")
    if not isinstance(stop_id, str) or not stop_id:
        raise HTTPException(status_code=400, detail=NEED_STOP)

    def write(state: dict) -> dict:
        cart = _find_cart(state, cart_id)
        if not _stored_items(cart):
            raise HTTPException(status_code=400, detail=NOT_LOADED)
        stop = next((item for item in _stored_stops(cart) if item.get("id") == stop_id), None)
        if stop is None:
            raise HTTPException(status_code=400, detail=NEED_STOP)
        lines = _lines(cart, body.get("lines"))
        order = {
            "id": uuid.uuid4().hex,
            "driver_token": _driver_token(state),
            "cart_id": cart_id,
            "cart_name": cart.get("name"),
            "schedule": stop.get("schedule"),
            "address": stop.get("address"),
            "lines": lines,
            "status": "open",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        state["orders"].insert(0, order)
        return {"slip": _public_slip(order)}

    return _with_state(write, save=True)


@router.get("/slip")
def slip(request: Request) -> dict:
    role, order_id = _allow(request, "driver")
    if role != "driver" or not order_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)

    def read(state: dict) -> dict:
        return {"slip": _public_slip(_find_order(state, order_id))}

    return _with_state(read, save=False)


@router.post("/orders/arrive")
def arrive(request: Request) -> dict:
    _role, order_id = _allow(request, "driver")
    if not order_id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=WRONG_ROLE)

    def write(state: dict) -> dict:
        order = _find_order(state, order_id)
        if order.get("status") != "arrived":
            order["status"] = "arrived"
        return {"slip": _public_slip(order)}

    return _with_state(write, save=True)
