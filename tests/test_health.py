import asyncio
import json

from fastapi.responses import JSONResponse, RedirectResponse

import main


def test_readyz_returns_503_without_exposing_internal_paths(monkeypatch):
    async def database_down():
        return False

    monkeypatch.setattr(main, "ping_database", database_down)
    monkeypatch.setattr(main, "storage_ready", lambda: True)
    response = asyncio.run(main.readyz())
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    payload = json.loads(response.body)
    assert payload == {"status": "not_ready", "database_ready": False, "storage_ready": True}
    assert "/srv/" not in response.body.decode()


def test_readyz_returns_200_payload_when_dependencies_are_ready(monkeypatch):
    async def database_up():
        return True

    monkeypatch.setattr(main, "ping_database", database_up)
    monkeypatch.setattr(main, "storage_ready", lambda: True)
    assert asyncio.run(main.readyz()) == {
        "status": "ready",
        "database_ready": True,
        "storage_ready": True,
    }


def test_legacy_media_endpoint_redirects_without_serving_file():
    response = asyncio.run(main.serve_media("beats/abc/cover.webp"))
    assert isinstance(response, RedirectResponse)
    assert response.status_code == 308
    assert response.headers["location"] == "https://res.beatnow.app/beats/abc/cover.webp"
