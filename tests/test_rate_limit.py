import asyncio

import pytest
from fastapi import HTTPException

from core.rate_limit import _BUCKETS, enforce_rate_limit, get_client_ip


class DummyClient:
    host = "127.0.0.1"


class DummyRequest:
    client = DummyClient()
    headers = {}


def test_rate_limit_blocks_after_limit():
    _BUCKETS.clear()
    request = DummyRequest()
    for _ in range(2):
        asyncio.run(enforce_rate_limit(request, "test", limit=2, window=60))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(enforce_rate_limit(request, "test", limit=2, window=60))
    assert exc.value.status_code == 429


def test_forwarded_ip_is_only_used_for_trusted_proxy():
    request = DummyRequest()
    request.headers = {"x-forwarded-for": "203.0.113.5, 127.0.0.1"}
    assert get_client_ip(request) == "203.0.113.5"

    request.client = type("Client", (), {"host": "203.0.113.20"})()
    request.headers = {"x-forwarded-for": "198.51.100.10"}
    assert get_client_ip(request) == "203.0.113.20"
