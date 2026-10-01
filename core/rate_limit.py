from __future__ import annotations

import time
import ipaddress
from collections import defaultdict, deque
from typing import Deque, DefaultDict

from fastapi import HTTPException, Request, status

from config.settings import settings

_BUCKETS: DefaultDict[str, Deque[float]] = defaultdict(deque)


def _cleanup(bucket: Deque[float], now: float, window: int) -> None:
    while bucket and now - bucket[0] > window:
        bucket.popleft()


def get_client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer not in settings.trusted_proxy_ips:
        return peer
    forwarded = getattr(request, "headers", {}).get("x-forwarded-for", "")
    candidate = forwarded.split(",", 1)[0].strip()
    if not candidate:
        return peer
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return peer


async def enforce_rate_limit(request: Request, key: str, limit: int, window: int | None = None) -> None:
    window = window or settings.rate_limit_window_seconds
    identity = get_client_ip(request)
    bucket_key = f"{key}:{identity}"
    now = time.time()
    bucket = _BUCKETS[bucket_key]
    _cleanup(bucket, now, window)
    if len(bucket) >= limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
        )
    bucket.append(now)
