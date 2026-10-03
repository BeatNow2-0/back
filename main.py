from __future__ import annotations

from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi import HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pymongo.errors import PyMongoError

from config.changeStream import watch_changes
from config.db import DATABASE_CONFIGURATION_ERROR, ensure_indexes, handle_database_error, ping_database
from config.settings import settings
from core.exceptions import http_exception_handler, unhandled_exception_handler
from core.logging import configure_logging
from core.security_headers import SecurityHeadersMiddleware
from core.request_context import RequestContextMiddleware
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
from routes.beat_analysis_routes import router as beat_analysis_router, publish_router as beat_publish_router
from services.storage import StorageError, storage, storage_ready

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.database_error = DATABASE_CONFIGURATION_ERROR
    app.state.database_ready = await ensure_indexes()
    if not app.state.database_ready and app.state.database_error is None:
        app.state.database_error = "MongoDB unavailable during startup"
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

if settings.prometheus_enabled:
    Instrumentator().instrument(app).expose(app, endpoint="/metrics")
app.add_exception_handler(PyMongoError, handle_database_error)
app.add_exception_handler(HTTPException, http_exception_handler)
app.add_exception_handler(Exception, unhandled_exception_handler)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RequestContextMiddleware)
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
app.include_router(beat_analysis_router, prefix="/api/v1/beat-analysis", tags=["beat-analysis"])
app.include_router(beat_publish_router, prefix="/api/v1/beats", tags=["beats"])
app.include_router(routes_router)


@app.get("/healthz", tags=["health"])
async def healthz():
    return {"status": "ok", "environment": settings.environment}


@app.get("/readyz", tags=["health"])
async def readyz():
    database_ready = await ping_database()
    media_ready = storage_ready()
    ready = database_ready and media_ready
    payload = {
        "status": "ready" if ready else "not_ready",
        "database_ready": database_ready,
        "storage_ready": media_ready,
    }
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/beatnow/{requested_path:path}", include_in_schema=False)
async def serve_media(requested_path: str):
    try:
        target = storage.get_public_url(requested_path)
    except StorageError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return RedirectResponse(target, status_code=308)
