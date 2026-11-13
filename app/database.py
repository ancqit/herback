"""MongoDB client.

Same pool settings as junctionBack `app/database.py`: pymongo `MongoClient`
against `MONGODB_URL`, database name from `MONGODB_DATABASE`. Herback defaults
the database to `herb`, so this cluster can sit beside Junction's `junction`
database without sharing its collections.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from pymongo import MongoClient
from pymongo.database import Database

_UNSET_URL = "mongodb://invalid:27017"


@dataclass(frozen=True)
class MongoSettings:
    url: str
    database: str
    min_pool_size: int
    max_pool_size: int

    @property
    def configured(self) -> bool:
        return bool(self.url) and self.url != _UNSET_URL


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def load_settings() -> MongoSettings:
    url = (os.getenv("MONGODB_URL") or "").strip() or _UNSET_URL
    database = (os.getenv("MONGODB_DATABASE") or "").strip() or "herb"
    return MongoSettings(
        url=url,
        database=database,
        min_pool_size=_int_env("MONGODB_MIN_POOL_SIZE", 5),
        max_pool_size=_int_env("MONGODB_MAX_POOL_SIZE", 100),
    )


def mongo_configured() -> bool:
    return load_settings().configured


def mongo_missing_env() -> list[str]:
    if mongo_configured():
        return []
    return ["MONGODB_URL"]


def mongo_host() -> str:
    """Cluster host only. The user and password never leave the client."""
    if not mongo_configured():
        return ""
    return urlparse(load_settings().url).hostname or ""


@lru_cache(maxsize=4)
def _client(url: str, min_pool_size: int, max_pool_size: int) -> MongoClient:
    return MongoClient(
        url,
        maxPoolSize=max_pool_size,
        minPoolSize=min_pool_size,
        maxIdleTimeMS=60_000,
        serverSelectionTimeoutMS=10_000,
        retryWrites=True,
    )


def get_client() -> MongoClient:
    settings = load_settings()
    return _client(settings.url, settings.min_pool_size, settings.max_pool_size)


def get_database() -> Database:
    settings = load_settings()
    return get_client()[settings.database]


def ping() -> None:
    get_database().command("ping")
