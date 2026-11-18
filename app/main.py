import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from .binter_portal import router as binter_router
from .cart_portal import router as cart_router
from .cors_config import load_cors_origin_regex, load_cors_origins
from .database import load_settings, mongo_configured, mongo_host, mongo_missing_env, ping
from .media import router as media_router
from .r2 import bucket_cors, ensure_browser_cors, r2_configured, r2_missing_env
from .room import router as room_router
from .scrap_portal import router as scrap_router

_openapi_enabled = os.getenv("OPENAPI_ENABLED", "true").lower() in {"1", "true", "yes"}
_log = logging.getLogger(__name__)

@asynccontextmanager
async def _allow_browser_uploads(_app: FastAPI):
    if not os.getenv("PYTEST_CURRENT_TEST"):
        try:
            ensure_browser_cors()
        except Exception:
            _log.exception("The bucket CORS rule was not saved")
    yield


app = FastAPI(
    title="Herback",
    description="Storage API for herbant, scrap, binter, and cart. Presigned uploads go to Cloudflare R2. MongoDB uses the same client settings as junctionBack.",
    version="0.1.0",
    docs_url="/docs" if _openapi_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _openapi_enabled else None,
    lifespan=_allow_browser_uploads,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=load_cors_origins(),
    allow_origin_regex=load_cors_origin_regex(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(media_router)
app.include_router(media_router, prefix="/api")
app.include_router(scrap_router)
app.include_router(scrap_router, prefix="/api")
app.include_router(binter_router)
app.include_router(binter_router, prefix="/api")
app.include_router(cart_router)
app.include_router(cart_router, prefix="/api")
app.include_router(room_router, prefix="/api")


@app.get("/health")
def health() -> dict[str, str | bool | list[str]]:
    """Liveness for Render. Reports which secret names are missing, never values."""
    return {
        "status": "ok",
        "service": "herback",
        "r2_configured": r2_configured(),
        "r2_missing": r2_missing_env(),
        "bucket_cors_saved": bucket_cors["saved"],
        "bucket_cors_error": bucket_cors["error"],
        "api_key_configured": bool((os.getenv("HERB_API_KEY") or "").strip()),
        "binter_notifier_configured": bool((os.getenv("BINTER_NOTIFIER_TOKEN") or "").strip()),
        "binter_receiver_configured": bool((os.getenv("BINTER_RECEIVER_TOKEN") or "").strip()),
        "binter_admin_configured": bool((os.getenv("BINTER_ADMIN_TOKEN") or "").strip()),
        "cart_entity_configured": bool((os.getenv("CART_ENTITY_TOKEN") or "").strip()),
        "cart_loader_configured": bool((os.getenv("CART_LOADER_TOKEN") or "").strip()),
        "mongo_configured": mongo_configured(),
        "mongo_missing": mongo_missing_env(),
        "mongo_host": mongo_host(),
    }


@app.get("/mongo")
def mongo_check() -> dict[str, str | bool]:
    """Confirm the cluster URL. Does not return the connection string."""
    if not mongo_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MONGODB_URL is not set",
        )
    try:
        ping()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="MongoDB ping failed",
        ) from exc
    settings = load_settings()
    return {"ok": True, "database": settings.database, "host": mongo_host()}
