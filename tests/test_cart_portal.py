"""Cart portal stores carts in herback's R2 bucket. Pictures are presigned. No network."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

ENTITY = "entity-number"
LOADER = "loader-number"


class Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
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
    return bucket


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _schedule() -> dict:
    return {"days": ["Mon", "Wed", "Fri"], "start": "07:00", "end": "09:30", "note": "Park gate"}


def _create(token: str = "CartTok9") -> dict:
    created = client.post(
        "/cart/carts",
        json={"token": token, "name": "Park gate", "schedule": _schedule()},
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
    assert client.post("/cart/upload-url", json={"contentType": "image/jpeg"}, headers=_auth(ENTITY)).status_code == 403


def test_entity_sees_loaded_carts_and_the_driver_gets_a_token(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    cart = _create()
    assert cart["token"] == "CartTok9"
    assert cart["loaded"] is False
    assert client.get("/cart/carts", headers=_auth(ENTITY)).json() == {"carts": []}

    signed = client.post(
        "/cart/upload-url",
        json={"contentType": "image/jpeg"},
        headers=_auth(LOADER),
    ).json()
    added = client.post(
        f"/cart/carts/{cart['id']}/veggies",
        json={"name": "Spinach", "price": 40, "unit": "bunch", "photo": _photo(signed["object_key"])},
        headers=_auth(LOADER),
    )
    assert added.status_code == 201
    veggie = added.json()["cart"]["veggies"][0]
    assert veggie["photo"]["url"] == f"https://media.example/{signed['object_key']}"
    assert "object_key" not in added.text
    assert "cart/state.json" in bucket.objects

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
            "address": "12 Neem Lane, near the park",
            "lines": [{"veggie_id": veggie["id"], "qty": 2}],
        },
        headers=_auth(ENTITY),
    )
    assert ordered.status_code == 201
    slip = ordered.json()["slip"]
    assert slip["address"] == "12 Neem Lane, near the park"
    assert slip["lines"][0]["name"] == "Spinach"
    assert slip["lines"][0]["qty"] == 2
    assert slip["total"] == 80
    assert len(slip["driver_token"]) == 8
    assert slip["schedule"]["start"] == "07:00"

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
        json={"cart_id": cart["id"], "address": "12 Neem Lane, near the park", "lines": []},
        headers=_auth(LOADER),
    )
    assert refused.status_code == 403


def test_rejects_foreign_photo_keys_and_a_duplicate_cart_token(monkeypatch: pytest.MonkeyPatch):
    _install(monkeypatch)
    cart = _create("CartTok9")
    foreign = client.post(
        f"/cart/carts/{cart['id']}/veggies",
        json={
            "name": "Mint",
            "price": 10,
            "unit": "bunch",
            "photo": _photo("binter/photos/" + "b" * 32 + ".jpg"),
        },
        headers=_auth(LOADER),
    )
    assert foreign.status_code == 400
    herb = client.post(
        f"/cart/carts/{cart['id']}/veggies",
        json={
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
        json={"token": "CartTok9", "name": "Other", "schedule": _schedule()},
        headers=_auth(LOADER),
    )
    assert duplicate.status_code == 409
    assert client.post(
        "/cart/carts",
        json={"token": "no", "name": "Other", "schedule": _schedule()},
        headers=_auth(LOADER),
    ).status_code == 400


def test_a_cart_token_cannot_order_another_cart(monkeypatch: pytest.MonkeyPatch):
    bucket = _install(monkeypatch)
    first = _create("CartTok9")
    second = _create("CartTok8")
    photo = "cart/photos/" + "d" * 32 + ".jpg"
    for cart in (first, second):
        added = client.post(
            f"/cart/carts/{cart['id']}/veggies",
            json={"name": "Beans", "price": 60, "unit": "kg", "photo": _photo(photo)},
            headers=_auth(LOADER),
        )
        assert added.status_code == 201
        cart["veggie_id"] = added.json()["cart"]["veggies"][0]["id"]
    blocked = client.post(
        "/cart/orders",
        json={
            "cart_id": second["id"],
            "address": "12 Neem Lane, near the park",
            "lines": [{"veggie_id": second["veggie_id"], "qty": 1}],
        },
        headers=_auth("CartTok9"),
    )
    assert blocked.status_code == 403
    removed = client.delete(f"/cart/carts/{first['id']}/veggies/{first['veggie_id']}", headers=_auth(LOADER))
    assert removed.status_code == 204
    assert photo in bucket.deleted
    gone = client.delete(f"/cart/carts/{second['id']}", headers=_auth(LOADER))
    assert gone.status_code == 204
    assert client.get(f"/cart/carts/{second['id']}", headers=_auth(LOADER)).status_code == 404
