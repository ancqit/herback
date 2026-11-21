"""Cart forms live in Mongo with CDN URLs. Photographs stay in R2. No network."""

import copy

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ENTITY = "entity-number"
LOADER = "loader-number"


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.forms: dict[str, dict] = {}
        self.deleted: list[str] = []

    def read_object(self, key: str) -> bytes | None:
        return self.objects.get(key)

    def write_object(self, key: str, body: bytes, **_kwargs) -> None:
        self.objects[key] = body

    def delete_key(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)

    def presign_put_key(self, key: str, content_type: str) -> str:
        return f"https://example.r2.cloudflarestorage.com/{key}?type={content_type}"

    def public_url(self, key: str) -> str:
        return f"https://media.example/{key}"

    def require_r2(self) -> None:
        return None

    def load_form(self, name: str) -> dict | None:
        form = self.forms.get(name)
        return None if form is None else copy.deepcopy(form)

    def save_form(self, name: str, form: dict) -> None:
        self.forms[name] = copy.deepcopy(form)


def _install(monkeypatch: pytest.MonkeyPatch) -> Bucket:
    bucket = Bucket()
    monkeypatch.setenv("CART_ENTITY_TOKEN", ENTITY)
    monkeypatch.setenv("CART_LOADER_TOKEN", LOADER)
    monkeypatch.setattr("app.cart_portal.r2.read_object", bucket.read_object)
    monkeypatch.setattr("app.cart_portal.r2.write_object", bucket.write_object)
    monkeypatch.setattr("app.cart_portal.r2.delete_key", bucket.delete_key)
    monkeypatch.setattr("app.cart_portal.r2.presign_put_key", bucket.presign_put_key)
    monkeypatch.setattr("app.cart_portal.r2.public_url", bucket.public_url)
    monkeypatch.setattr("app.cart_portal.r2.require_r2", bucket.require_r2)
    monkeypatch.setattr("app.forms.load_form", bucket.load_form)
    monkeypatch.setattr("app.forms.save_form", bucket.save_form)
    return bucket


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _schedule(note: str = "Park gate", start: str = "07:00", end: str = "09:30") -> dict:
    return {"days": ["Mon", "Wed", "Fri"], "start": start, "end": end, "note": note}


def _stop(address: str = "12 Neem Lane, near the park", **schedule: str) -> dict:
    return {"address": address, "schedule": _schedule(**schedule)}


def _create(token: str = "CartTok9", stops: list[dict] | None = None) -> dict:
    created = client.post(
        "/cart/carts",
        json={"token": token, "name": "Park gate", "stops": stops or [_stop()]},
        headers=_auth(LOADER),
    )
    assert created.status_code == 201
    return created.json()["cart"]


def _photo(key: str = "cart/photos/" + "a" * 32 + ".jpg") -> dict:
    return {"object_key": key, "content_type": "image/jpeg", "filename": "spinach.jpg"}


def test_doors_and_a_shut_number(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    entity = client.post("/cart/login", json={"token": f"  {ENTITY}  "})
    loader = client.post("/api/cart/login", json={"token": LOADER})
    assert entity.status_code == 200
    assert entity.json() == {"ok": True, "role": "entity"}
    assert loader.status_code == 200
    assert loader.json()["role"] == "loader"
    assert client.post("/cart/login", json={"token": ""}).status_code == 401
    assert client.post("/cart/login", json={"token": "nope"}).json()["detail"] == (
        "That number does not open the portal."
    )
    monkeypatch.setenv("CART_ENTITY_TOKEN", "")
    assert client.post("/cart/login", json={"token": ENTITY}).status_code == 401


def test_health_names_the_cart_doors_without_the_numbers(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CART_ENTITY_TOKEN", ENTITY)
    monkeypatch.setenv("CART_LOADER_TOKEN", LOADER)
    response = client.get("/health")
    body = response.json()
    assert body["cart_entity_configured"] is True
    assert body["cart_loader_configured"] is True
    assert ENTITY not in response.text
    assert LOADER not in response.text
    monkeypatch.setenv("CART_LOADER_TOKEN", "   ")
    assert client.get("/health").json()["cart_loader_configured"] is False


def test_a_cart_photograph_is_an_image(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    cart = _create()
    key = "cart/photos/" + "e" * 32 + ".png"
    bucket.objects[key] = b"\x89PNG spinach"
    added = client.post(
        f"/cart/carts/{cart['id']}/items",
        json={
            "kind": "veggie",
            "name": "Spinach",
            "price": 40,
            "unit": "bunch",
            "photo": {"object_key": key, "content_type": "image/png", "filename": "spinach.png"},
        },
        headers=_auth(LOADER),
    )
    assert added.status_code == 201
    name = key.rsplit("/", 1)[-1]
    assert added.json()["cart"]["items"][0]["photo"]["url"].endswith(f"/cart/photos/{name}")
    got = client.get(f"/cart/photos/{name}")
    assert got.status_code == 200
    assert got.headers["content-type"].startswith("image/png")
    assert got.content == b"\x89PNG spinach"
    assert got.headers["content-disposition"] == "inline"
    assert client.get("/cart/photos/" + "f" * 32 + ".jpg").status_code == 404
    assert client.get("/cart/photos/not-a-photo.jpg").status_code == 404


def test_photo_keys_stay_under_cart_photos(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    signed = client.post(
        "/cart/upload-url",
        json={"contentType": "image/jpeg", "filename": "spinach.jpg"},
        headers=_auth(LOADER),
    )
    assert signed.status_code == 200
    body = signed.json()
    assert body["object_key"].startswith("cart/photos/")
    assert body["object_key"].endswith(".jpg")
    assert body["max_bytes"] == 8 * 1024 * 1024
    assert "herb/" not in body["object_key"]
    assert "2001/" not in body["object_key"]
    assert "binter/" not in body["object_key"]
    assert client.post("/cart/upload-url", json={"contentType": "image/gif"}, headers=_auth(LOADER)).status_code == 400
    entity_photo = client.post(
        "/cart/upload-url",
        json={"contentType": "image/jpeg", "filename": "spinach.jpg"},
        headers=_auth(ENTITY),
    )
    assert entity_photo.status_code == 200
    assert entity_photo.json()["object_key"].startswith("cart/photos/")


def test_entity_sees_loaded_carts_and_the_driver_gets_a_token(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    cart = _create(
        stops=[
            _stop(),
            _stop("4 Market Road, beside the well", note="Market", start="16:00", end="18:00"),
        ]
    )
    assert cart["token"] == "CartTok9"
    assert cart["loaded"] is False
    assert len(cart["stops"]) == 2
    assert cart["stops"][1]["schedule"]["start"] == "16:00"
    assert client.get("/cart/carts", headers=_auth(ENTITY)).json() == {"carts": []}

    signed = client.post(
        "/cart/upload-url",
        json={"contentType": "image/jpeg"},
        headers=_auth(LOADER),
    ).json()
    added = client.post(
        f"/cart/carts/{cart['id']}/items",
        json={
            "kind": "veggie",
            "name": "Spinach",
            "price": 40,
            "unit": "bunch",
            "photo": _photo(signed["object_key"]),
        },
        headers=_auth(LOADER),
    )
    assert added.status_code == 201
    veggie = added.json()["cart"]["items"][0]
    assert veggie["kind"] == "veggie"
    photo_name = signed["object_key"].rsplit("/", 1)[-1]
    assert veggie["photo"]["url"] == f"https://herback-1.onrender.com/cart/photos/{photo_name}"
    assert "object_key" not in added.text
    stored = bucket.forms["cart"]["carts"][0]["items"][0]["photo"]
    assert stored["cdn"] == f"https://media.example/{signed['object_key']}"
    assert "cart/state.json" not in bucket.objects
    other = client.post(
        f"/cart/carts/{cart['id']}/items",
        json={
            "kind": "other",
            "name": "Honey",
            "price": 120,
            "unit": "piece",
            "photo": _photo(signed["object_key"]),
        },
        headers=_auth(LOADER),
    )
    assert other.status_code == 201
    assert other.json()["cart"]["items"][1]["kind"] == "other"

    listed = client.get("/api/cart/carts", headers=_auth(ENTITY))
    assert listed.status_code == 200
    assert listed.json()["carts"][0]["name"] == "Park gate"
    assert "CartTok9" not in listed.text

    opened = client.post("/cart/login", json={"token": "CartTok9"})
    assert opened.status_code == 200
    assert opened.json()["role"] == "cart"
    assert opened.json()["cart_id"] == cart["id"]

    ordered = client.post(
        "/cart/orders",
        json={
            "cart_id": cart["id"],
            "stop_id": cart["stops"][1]["id"],
            "lines": [
                {"item_id": veggie["id"], "qty": 2},
                {"item_id": other.json()["cart"]["items"][1]["id"], "qty": 1},
            ],
        },
        headers=_auth(ENTITY),
    )
    assert ordered.status_code == 201
    slip = ordered.json()["slip"]
    assert slip["address"] == "4 Market Road, beside the well"
    assert slip["lines"][0]["name"] == "Spinach"
    assert slip["lines"][0]["qty"] == 2
    assert slip["lines"][1]["kind"] == "other"
    assert slip["total"] == 200
    assert len(slip["driver_token"]) == 8
    assert slip["schedule"]["start"] == "16:00"

    driver = client.post("/cart/login", json={"token": slip["driver_token"]})
    assert driver.status_code == 200
    assert driver.json()["role"] == "driver"
    fetched = client.get("/cart/slip", headers=_auth(slip["driver_token"]))
    assert fetched.json()["slip"]["cart_name"] == "Park gate"
    arrived = client.post("/cart/orders/arrive", headers=_auth(slip["driver_token"]))
    assert arrived.status_code == 200
    assert arrived.json()["slip"]["status"] == "arrived"
    again = client.post("/cart/orders/arrive", headers=_auth(slip["driver_token"]))
    assert again.json()["slip"]["status"] == "arrived"

    refused = client.post(
        "/cart/orders",
        json={"cart_id": cart["id"], "stop_id": cart["stops"][0]["id"], "lines": []},
        headers=_auth(LOADER),
    )
    assert refused.status_code == 403


def test_rejects_foreign_photo_keys_and_a_duplicate_cart_token(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    cart = _create("CartTok9")
    foreign = client.post(
        f"/cart/carts/{cart['id']}/items",
        json={
            "kind": "veggie",
            "name": "Mint",
            "price": 10,
            "unit": "bunch",
            "photo": _photo("binter/photos/" + "b" * 32 + ".jpg"),
        },
        headers=_auth(LOADER),
    )
    assert foreign.status_code == 400
    herb = client.post(
        f"/cart/carts/{cart['id']}/items",
        json={
            "kind": "veggie",
            "name": "Mint",
            "price": 10,
            "unit": "bunch",
            "photo": _photo("herb/images/" + "c" * 32 + ".jpg"),
        },
        headers=_auth(LOADER),
    )
    assert herb.status_code == 400
    duplicate = client.post(
        "/cart/carts",
        json={"token": "CartTok9", "name": "Other", "stops": [_stop()]},
        headers=_auth(LOADER),
    )
    assert duplicate.status_code == 409
    assert client.post(
        "/cart/carts",
        json={"token": "no", "name": "Other", "stops": [_stop()]},
        headers=_auth(LOADER),
    ).status_code == 400


def test_the_selected_cart_adds_a_vegetable_and_another_thing(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    cart = _create("CartTok9")
    other = _create("CartTok8")
    photo = _photo()
    added = client.post(
        f"/cart/carts/{cart['id']}/items",
        json={"kind": "veggie", "name": "Spinach", "price": 40, "unit": "bunch", "photo": photo},
        headers=_auth("CartTok9"),
    )
    assert added.status_code == 201
    item = added.json()["cart"]["items"][0]
    assert item["kind"] == "veggie"
    assert item["name"] == "Spinach"
    honey = client.post(
        f"/cart/carts/{cart['id']}/items",
        json={"kind": "other", "name": "Honey", "price": 120, "unit": "piece", "photo": photo},
        headers=_auth(ENTITY),
    )
    assert honey.status_code == 201
    assert honey.json()["cart"]["items"][1]["kind"] == "other"
    blocked = client.post(
        f"/cart/carts/{other['id']}/items",
        json={"kind": "veggie", "name": "Mint", "price": 10, "unit": "bunch", "photo": photo},
        headers=_auth("CartTok9"),
    )
    assert blocked.status_code == 403
    removed = client.delete(
        f"/cart/carts/{cart['id']}/items/{item['id']}",
        headers=_auth("CartTok9"),
    )
    assert removed.status_code == 204
    left = client.get(f"/cart/carts/{cart['id']}", headers=_auth(ENTITY))
    assert [row["name"] for row in left.json()["cart"]["items"]] == ["Honey"]


def test_a_cart_token_cannot_order_another_cart(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    first = _create("CartTok9")
    second = _create("CartTok8")
    photo = "cart/photos/" + "d" * 32 + ".jpg"
    for cart in (first, second):
        added = client.post(
            f"/cart/carts/{cart['id']}/items",
            json={"kind": "veggie", "name": "Beans", "price": 60, "unit": "kg", "photo": _photo(photo)},
            headers=_auth(LOADER),
        )
        assert added.status_code == 201
        cart["item_id"] = added.json()["cart"]["items"][0]["id"]
        cart["stop_id"] = added.json()["cart"]["stops"][0]["id"]
    blocked = client.post(
        "/cart/orders",
        json={
            "cart_id": second["id"],
            "stop_id": second["stop_id"],
            "lines": [{"item_id": second["item_id"], "qty": 1}],
        },
        headers=_auth("CartTok9"),
    )
    assert blocked.status_code == 403
    removed = client.delete(f"/cart/carts/{first['id']}/items/{first['item_id']}", headers=_auth(LOADER))
    assert removed.status_code == 204
    assert photo in bucket.deleted
    gone = client.delete(f"/cart/carts/{second['id']}", headers=_auth(LOADER))
    assert gone.status_code == 204
    assert client.get(f"/cart/carts/{second['id']}", headers=_auth(LOADER)).status_code == 404
