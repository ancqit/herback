import os

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from .cors_config import load_cors_origin_regex, load_cors_origins
from .media import router as media_router
from .r2 import r2_configured, r2_missing_env

_openapi_enabled = os.getenv("OPENAPI_ENABLED", "true").lower() in {"1", "true", "yes"}

app = FastAPI(
    title="Herback",
    description="Storage API for herbant. Presigned uploads go to Cloudflare R2.",
    version="0.1.0",
    docs_url="/docs" if _openapi_enabled else None,
    redoc_url=None,
    openapi_url="/openapi.json" if _openapi_enabled else None,
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


@app.get("/health")
def health() -> dict[str, str | bool | list[str]]:
    """Liveness for Render. Reports which secret names are missing, never values."""
    return {
        "status": "ok",
        "service": "herback",
        "r2_configured": r2_configured(),
        "r2_missing": r2_missing_env(),
        "api_key_configured": bool((os.getenv("HERB_API_KEY") or "").strip()),
    }
