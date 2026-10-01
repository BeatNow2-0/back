from __future__ import annotations

import logging

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


def _error_code(status_code: int, message: str) -> str:
    lowered = message.lower()
    if status_code == 422 and "identifier" in lowered:
        return "invalid_identifier"
    if status_code == 401:
        return "authentication_failed"
    if status_code == 403:
        return "forbidden"
    if status_code == 404:
        return "not_found"
    if status_code == 413:
        return "upload_too_large"
    if status_code == 415:
        return "invalid_upload"
    if status_code == 429:
        return "rate_limit_exceeded"
    return "request_failed"


async def http_exception_handler(request: Request, exc: HTTPException):
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    headers = exc.headers or {}
    return JSONResponse(
        status_code=exc.status_code,
        headers=headers,
        content={
            "detail": exc.detail,
            "error": _error_code(exc.status_code, message),
            "message": message,
            "details": {},
            "request_id": _request_id(request),
        },
    )


async def unhandled_exception_handler(request: Request, exc: Exception):
    logger.exception("Unhandled exception on %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "detail": "Internal server error",
            "error": "internal_error",
            "message": "Internal server error",
            "details": {},
            "request_id": _request_id(request),
        },
    )
