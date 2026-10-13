from __future__ import annotations

import asyncio
import hashlib
import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException, UploadFile
from starlette.requests import Request

from model.user_shemas import CurrentUser
from routes import posts_routes
from services import beat_audio
from services.storage import with_post_media_urls

POST_ID = "507f1f77bcf86cd799439099"
OWNER_ID = "507f1f77bcf86cd799439011"


def _user(user_id: str = OWNER_ID) -> CurrentUser:
    return CurrentUser(_id=user_id, username="producer", email="producer@example.com", password="hash", is_active=True)


def _request():
    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("127.0.0.1", 4567), "server": ("test", 80), "scheme": "http", "query_string": b""})


def test_audio_upload_hashes_exact_master_and_creates_separate_preview(monkeypatch, tmp_path):
    master = b"RIFF" + b"original-master-bytes"
    monkeypatch.setattr(beat_audio.settings, "beats_original_dir", tmp_path / "private")
    monkeypatch.setattr(beat_audio.settings, "max_audio_upload_size", 1024)
    monkeypatch.setattr(beat_audio.settings, "beat_analysis_max_duration_seconds", 600)
    monkeypatch.setattr(beat_audio.shutil, "which", lambda name: f"/usr/bin/{name}")

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if Path(command[0]).name == "ffprobe":
            metadata = {"format": {"duration": "4.0", "format_name": "wav"}, "streams": [{"codec_type": "audio"}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")
        Path(command[-1]).write_bytes(b"streaming-preview")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(beat_audio.subprocess, "run", fake_run)
    upload = UploadFile(filename="master.wav", file=io.BytesIO(master), headers={"content-type": "audio/wav"})
    audio = asyncio.run(beat_audio.prepare_uploaded_audio(upload, OWNER_ID))

    assert audio.staged_original.read_bytes() == master
    assert audio.original_sha256 == hashlib.sha256(master).hexdigest()
    assert audio.staged_original != audio.staged_preview
    assert audio.staged_preview.read_bytes() == b"streaming-preview"
    assert audio.master_key.startswith(f"{OWNER_ID}/")
    assert audio.database_fields["processing_status"] == "ready"
    ffmpeg_command = next(command for command in commands if Path(command[0]).name == "ffmpeg")
    assert ffmpeg_command[ffmpeg_command.index("-t") + 1] == "4.0"


def test_preview_bounds_are_validated_against_real_duration(monkeypatch, tmp_path):
    source = tmp_path / "source.wav"
    source.write_bytes(b"RIFFaudio")
    monkeypatch.setattr(beat_audio.shutil, "which", lambda name: f"/usr/bin/{name}")

    commands = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if Path(command[0]).name == "ffprobe":
            metadata = {"format": {"duration": "60.0", "format_name": "wav", "bit_rate": "64000"}, "streams": [{"codec_type": "audio"}]}
            return subprocess.CompletedProcess(command, 0, json.dumps(metadata), "")
        Path(command[-1]).write_bytes(b"preview")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(beat_audio.subprocess, "run", fake_run)
    with pytest.raises(HTTPException) as error:
        beat_audio.process_audio_file(source, tmp_path / "invalid.mp3", "wav", 50, 61)
    assert error.value.status_code == 422

    duration, _ = beat_audio.process_audio_file(source, tmp_path / "valid.mp3", "wav", 10, 25)
    assert duration == 60
    ffmpeg_command = [command for command in commands if Path(command[0]).name == "ffmpeg"][-1]
    assert ffmpeg_command[ffmpeg_command.index("-b:a") + 1] == "48k"


@pytest.mark.parametrize(
    "filename,content_type,data,expected_status",
    [
        ("../escape.wav", "audio/wav", b"RIFFanything", 415),
        ("track.wav", "text/plain", b"RIFFanything", 415),
        ("track.exe", "application/octet-stream", b"RIFFanything", 415),
    ],
)
def test_audio_upload_rejects_traversal_disguised_or_unsupported(monkeypatch, tmp_path, filename, content_type, data, expected_status):
    monkeypatch.setattr(beat_audio.settings, "beats_original_dir", tmp_path / "private")
    upload = UploadFile(filename=filename, file=io.BytesIO(data), headers={"content-type": content_type})
    with pytest.raises(HTTPException) as error:
        asyncio.run(beat_audio.prepare_uploaded_audio(upload, OWNER_ID))
    assert error.value.status_code == expected_status


def test_audio_upload_enforces_limit_before_processing(monkeypatch, tmp_path):
    monkeypatch.setattr(beat_audio.settings, "beats_original_dir", tmp_path / "private")
    monkeypatch.setattr(beat_audio.settings, "max_audio_upload_size", 4)
    upload = UploadFile(filename="track.wav", file=io.BytesIO(b"12345"), headers={"content-type": "audio/wav"})
    with pytest.raises(HTTPException) as error:
        asyncio.run(beat_audio.prepare_uploaded_audio(upload, OWNER_ID))
    assert error.value.status_code == 413


def test_public_media_payload_never_contains_original_metadata():
    result = with_post_media_urls({
        "_id": POST_ID,
        "user_id": OWNER_ID,
        "preview_key": "previews/beat/preview.mp3",
        "master_key": f"{OWNER_ID}/unpredictable.wav",
        "processing_status": "ready",
        "original_sha256": "private-hash",
        "temporary_original_path": "/srv/private/original.wav",
    })
    assert result["audio_url"].endswith("/previews/beat/preview.mp3")
    assert result["preview_url"] == result["audio_url"]
    assert "master_key" not in result
    assert "original_storage_key" not in result
    assert "original_sha256" not in result
    assert "temporary_original_path" not in result


class FakePosts:
    def __init__(self, document):
        self.document = document

    async def find_one(self, query, projection=None):
        return dict(self.document) if self.document and self.document["_id"] == query["_id"] else None


def test_no_master_download_route_exists_even_for_owner():
    paths = {route.path for route in posts_routes.router.routes}
    assert "/{post_id}/original" not in paths


def test_failed_or_legacy_audio_never_gets_a_public_url():
    failed = with_post_media_urls({"_id": POST_ID, "audio_key": "beats/id/audio.wav", "processing_status": "failed"})
    legacy = with_post_media_urls({"_id": POST_ID, "audio_key": "beats/id/audio.wav", "processing_status": "ready"})
    assert failed["audio_url"] is None
    assert legacy["audio_url"] is None


def test_preview_stream_is_public_and_redirects_only_to_preview(monkeypatch):
    post = {
        "_id": ObjectId(POST_ID),
        "user_id": OWNER_ID,
        "publication_status": "published",
        "processing_status": "ready",
        "preview_key": f"previews/{POST_ID}/preview.mp3",
        "visibility": "published",
    }
    monkeypatch.setattr(posts_routes, "post_collection", FakePosts(post))

    async def no_limit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(posts_routes, "enforce_rate_limit", no_limit)
    response = asyncio.run(posts_routes.stream_preview(POST_ID, _request()))
    assert response.status_code == 307
    assert response.headers["location"].endswith(f"/previews/{POST_ID}/preview.mp3")
