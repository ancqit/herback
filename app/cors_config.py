"""Browser origins allowed to call herback.

herbant and scrap are the Angular apps on Vercel. Env origins are merged
with the built-in list, so setting CORS_ORIGINS never drops localhost or the live sites.
"""

from __future__ import annotations

import os
import re

DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:4200",
    "http://127.0.0.1:4200",
    "https://herbant.vercel.app",
    "https://scrap-sooty.vercel.app",
)

# Vercel preview hosts for herbant and scrap.
_DEFAULT_ORIGIN_REGEX = r"^https://(?:herbant|scrap)[a-z0-9-]*\.vercel\.app$"


def load_cors_origins() -> list[str]:
    origins: list[str] = list(DEFAULT_CORS_ORIGINS)
    raw = os.getenv("CORS_ORIGINS", "").strip()
    if raw:
        for part in raw.split(","):
            origin = part.strip().rstrip("/")
            if origin and origin not in origins:
                origins.append(origin)
    return origins


def load_cors_origin_regex() -> str | None:
    raw = os.getenv("CORS_ORIGIN_REGEX", _DEFAULT_ORIGIN_REGEX).strip()
    if not raw:
        return None
    try:
        re.compile(raw)
    except re.error:
        return None
    return raw
