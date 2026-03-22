import asyncio

import pytest
from fastapi import HTTPException

from core.rate_limit import _BUCKETS, enforce_rate_limit


class DummyClient:
    host = "127.0.0.1"


class DummyRequest:
    client = DummyClient()


def test_rate_limit_blocks_after_limit():
    _BUCKETS.clear()
    request = DummyRequest()
    for _ in range(2):
        asyncio.run(enforce_rate_limit(request, "test", limit=2, window=60))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(enforce_rate_limit(request, "test", limit=2, window=60))
    assert exc.value.status_code == 429
