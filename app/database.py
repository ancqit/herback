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
    """Same check junctionBack uses before it treats the cluster as up: `ping`."""
    get_database().command("ping")


def mongo_probe() -> tuple[bool, str]:
    """Ping the cluster. Returns `(ok, error name)`.

    A bad `mongodb+srv` host raises `ConfigurationError` while the client is
    constructed, before `ping` runs. That used to escape `/health` as a 500.
    The error name is the exception class only. The URL never leaves this function.
    """
    if not mongo_configured():
        return False, ""
    settings = load_settings()
    client = None
    try:
        client = MongoClient(
            settings.url,
            serverSelectionTimeoutMS=2_000,
            connectTimeoutMS=2_000,
            retryWrites=True,
        )
        client[settings.database].command("ping")
        return True, ""
    except Exception as exc:
        return False, type(exc).__name__
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass


def mongo_ok() -> bool:
    """True when the cluster answers ping. A miss stays false and does not raise."""
    ok, _error = mongo_probe()
    return ok
