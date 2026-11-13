"""Herbant room snapshots.

The portal keeps one picture in memory so the screens stay quick. This collection
is the dump. A room keeps the newest 24 pictures; older dumps are removed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from .database import get_database, mongo_configured

router = APIRouter(tags=["room"])

KEPT_DUMPS = 24
DEFAULT_ROOM = "herbalife"


class RoomDump(BaseModel):
    room: str = Field(default=DEFAULT_ROOM, min_length=1, max_length=80)
    snapshot: dict[str, Any]


def require_mongo() -> None:
    if not mongo_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="MONGODB_URL is not set")


def snapshots():
    require_mongo()
    collection = get_database()["room_snapshots"]
    collection.create_index([("room", 1), ("at", -1)])
    return collection


def _require_snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="snapshot is required")
    if not isinstance(raw.get("id"), str) or not isinstance(raw.get("at"), str):
        raise HTTPException(status_code=400, detail="snapshot id and at are required")
    plans = raw.get("plans")
    diary = raw.get("diary")
    schedule = raw.get("schedule")
    if not isinstance(plans, dict) or not isinstance(plans.get("admin"), dict) or not isinstance(plans.get("user"), dict):
        raise HTTPException(status_code=400, detail="snapshot plans are required")
    if not isinstance(diary, dict) or not isinstance(diary.get("admin"), list) or not isinstance(diary.get("user"), list):
        raise HTTPException(status_code=400, detail="snapshot diary is required")
    if not isinstance(schedule, dict) or not isinstance(schedule.get("admin"), list) or not isinstance(schedule.get("user"), list):
        raise HTTPException(status_code=400, detail="snapshot schedule is required")
    if not isinstance(raw.get("added"), list):
        raise HTTPException(status_code=400, detail="snapshot added stops are required")
    centre = raw.get("centre")
    if not isinstance(centre, dict) or not isinstance(centre.get("name"), str):
        raise HTTPException(status_code=400, detail="snapshot centre is required")
    return raw


def remember_dump(collection, room: str, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Insert one picture and drop dumps past the cap. The stored object is the client's snapshot."""
    saved_at = datetime.now(timezone.utc).isoformat()
    collection.insert_one(
        {
            "room": room,
            "id": snapshot["id"],
            "at": snapshot["at"],
            "snapshot": snapshot,
            "saved_at": saved_at,
        }
    )
    stale = [
        item["_id"]
        for item in collection.find({"room": room}, {"_id": 1, "at": 1}).sort("at", -1).skip(KEPT_DUMPS)
    ]
    if stale:
        collection.delete_many({"_id": {"$in": stale}})
    return {"ok": True, "id": snapshot["id"], "at": snapshot["at"], "saved_at": saved_at}


def latest_dump(collection, room: str) -> dict[str, Any] | None:
    document = collection.find_one({"room": room}, sort=[("at", -1)])
    if document is None:
        return None
    snapshot = document.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    return snapshot


def schedule_rows(snapshot: dict[str, Any], room: str) -> list[dict[str, str]]:
    """The stops herbant reads: admin days, user days, and added stops as shared."""
    rows: list[dict[str, str]] = []
    schedule = snapshot.get("schedule") if isinstance(snapshot.get("schedule"), dict) else {}
    for audience in ("admin", "user"):
        stops = schedule.get(audience) if isinstance(schedule.get(audience), list) else []
        for stop in stops:
            row = _row(stop, audience, room)
            if row:
                rows.append(row)
    added = snapshot.get("added") if isinstance(snapshot.get("added"), list) else []
    for stop in added:
        row = _row(stop, "shared", room)
        if row:
            rows.append(row)
    rows.sort(key=lambda stop: (stop["date"], stop["time"], stop["products"]))
    return rows


def _row(stop: object, audience: str, room: str) -> dict[str, str] | None:
    if not isinstance(stop, dict):
        return None
    date = stop.get("date")
    time = stop.get("time")
    centre = stop.get("centre")
    products = stop.get("products")
    if not all(isinstance(value, str) for value in (date, time, centre, products)):
        return None
    ident = stop.get("id")
    if not isinstance(ident, str) or not ident:
        ident = f"{audience}-{date}-{time}"
    return {
        "id": ident,
        "room": room,
        "date": date,
        "time": time,
        "centre": centre,
        "products": products,
        "audience": audience,
    }


@router.post("/room")
def save_room(payload: RoomDump) -> dict[str, str | bool]:
    collection = snapshots()
    snapshot = _require_snapshot(payload.snapshot)
    return remember_dump(collection, payload.room.strip(), snapshot)


@router.get("/room")
def read_room(room: str = Query(default=DEFAULT_ROOM, min_length=1, max_length=80)) -> dict[str, Any]:
    snapshot = latest_dump(snapshots(), room.strip())
    if snapshot is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No snapshot yet")
    return {"snapshot": snapshot}


@router.get("/schedules")
def read_schedules(room: str = Query(default=DEFAULT_ROOM, min_length=1, max_length=80)) -> list[dict[str, str]]:
    """Schedule days from the latest dump. An empty list leaves the portal's picture alone."""
    snapshot = latest_dump(snapshots(), room.strip())
    if snapshot is None:
        return []
    return schedule_rows(snapshot, room.strip())
