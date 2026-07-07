from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from prometheus_client import start_http_server
from pymongo.errors import PyMongoError

from config.changeStream import watch_changes
from config.db import DATABASE_CONFIGURATION_ERROR, ensure_indexes, handle_database_error
from config.settings import settings
from core.exceptions import unhandled_exception_handler
from core.logging import configure_logging
from core.security_headers import SecurityHeadersMiddleware
from routes.download_routes import router as download_router
from routes.filter_routes import router as filter_router
from routes.follow_routes import router as follow_router
from routes.interactions_routes import router as interactions_router
from routes.lyrics_routes import router as lyrics_routes
from routes.mail_routes import router as mail_router
from routes.posts_routes import router as posts_router
from routes.routes import router as routes_router
from routes.search_routes import router as search_router
from routes.users_routes import router as users_router

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.database_error = DATABASE_CONFIGURATION_ERROR
    app.state.database_ready = await ensure_indexes()
    if not app.state.database_ready and app.state.database_error is None:
        app.state.database_error = "MongoDB unavailable during startup"
    if settings.prometheus_enabled:
        start_http_server(settings.prometheus_port)
    app.state.change_stream_task = None
    if (
        app.state.database_ready
        and settings.environment != "test"
        and settings.enable_change_stream_sync
    ):
        try:
            import asyncio

            app.state.change_stream_task = asyncio.create_task(watch_changes())
        except Exception:
            app.state.change_stream_task = None
    yield
    task = getattr(app.state, "change_stream_task", None)
    if task:
        task.cancel()


app = FastAPI(title=settings.app_name, debug=settings.debug, lifespan=lifespan)
app.add_exception_handler(PyMongoError, handle_database_error)
app.add_exception_handler(Exception, unhandled_exception_handler)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID"],
    max_age=600,
)

app.include_router(users_router, prefix="/v1/api/users", tags=["users"])
app.include_router(posts_router, prefix="/v1/api/posts", tags=["posts"])
app.include_router(interactions_router, prefix="/v1/api/interactions", tags=["interactions"])
app.include_router(lyrics_routes, prefix="/v1/api/lyrics", tags=["lyrics"])
app.include_router(follow_router, prefix="/v1/api/follows", tags=["follows"])
app.include_router(search_router, prefix="/v1/api/search", tags=["search"])
app.include_router(filter_router, prefix="/v1/api/filter", tags=["filter"])
app.include_router(mail_router, prefix="/v1/api/mail", tags=["mail"])
app.include_router(download_router, prefix="/v1/api/download", tags=["download"])
app.include_router(routes_router)


@app.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok", "environment": settings.environment}


@app.get("/readyz", tags=["health"])
async def readyz():
    return {
        "status": "ready" if getattr(app.state, "database_ready", False) else "degraded",
        "database_ready": getattr(app.state, "database_ready", False),
        "database_error": getattr(app.state, "database_error", None),
    }


def _resolve_media_path(requested_path: str) -> Path:
    base = settings.media_root.resolve()
    candidate = (base / requested_path).resolve()

    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc

    if candidate.exists() and candidate.is_file():
        return candidate

    # Backward compatibility: some clients still request photo_profile.png even
    # when the stored image is jpg/webp.
    if candidate.name == "photo_profile.png":
        fallback_matches = sorted(candidate.parent.glob("photo_profile.*"))
        for fallback in fallback_matches:
            if fallback.is_file():
                return fallback

    raise HTTPException(status_code=404, detail="File not found")


@app.get("/beatnow/{requested_path:path}", include_in_schema=False)
async def serve_media(requested_path: str):
    media_file = _resolve_media_path(requested_path)
    return FileResponse(media_file)
