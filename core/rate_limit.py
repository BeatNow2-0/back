from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Deque, DefaultDict

from fastapi import HTTPException, Request, status

from config.settings import settings

_BUCKETS: DefaultDict[str, Deque[float]] = defaultdict(deque)


def _cleanup(bucket: Deque[float], now: float, window: int) -> None:
    while bucket and now - bucket[0] > window:
        bucket.popleft()


async def enforce_rate_limit(request: Request, key: str, limit: int, window: int | None = None) -> None:
    window = window or settings.rate_limit_window_seconds
    identity = request.client.host if request.client else "unknown"
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
