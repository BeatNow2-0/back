import asyncio
import io
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile

from model.user_shemas import CurrentUser
from routes import beat_analysis_routes as routes


UID = "507f1f77bcf86cd799439011"
USER = CurrentUser(_id=UID, username="owner", email="x@example.com", password="x", is_active=True)


def wav_bytes():
    out = io.BytesIO()
    with wave.open(out, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(b"\0\0" * 800)
    return out.getvalue()


class Collection:
    def __init__(self, docs=None):
        self.docs = docs or {}

    async def insert_one(self, doc):
        self.docs[doc["_id"]] = doc
        return SimpleNamespace(inserted_id=doc["_id"])

    async def update_one(self, query, update):
        doc = self.docs.get(query["_id"])
        if doc:
            doc.update(update["$set"])
        return SimpleNamespace(matched_count=bool(doc))

    async def find_one(self, query):
        doc = self.docs.get(query["_id"])
        return doc if doc and doc.get("user_id") == query.get("user_id") else None

    def find(self, query):
        matches = [d for d in self.docs.values() if d["status"] not in ("published", "discarded", "expired") and d["expires_at"] <= query["expires_at"]["$lte"]]
        class Cursor:
            def __aiter__(self): self.items = iter(matches); return self
            async def __anext__(self):
                try: return next(self.items)
                except StopIteration: raise StopAsyncIteration
        return Cursor()


def test_upload_valid_audio_and_persist_analysis(monkeypatch, tmp_path):
    collection = Collection()
    monkeypatch.setattr(routes, "beat_analyses_collection", collection)
    monkeypatch.setattr(routes.settings, "media_root", tmp_path)
    async def user_id(_): return UID
    monkeypatch.setattr(routes, "get_user_id", user_id)
    monkeypatch.setattr(routes, "analyze_beat", lambda src, dst: {"audio": {"duration_seconds": .1}, "musical": {}, "loudness": {}, "stereo": {}, "frequency_balance": {}, "preview": {}, "insights": []})
    upload = UploadFile(filename="beat.wav", file=io.BytesIO(wav_bytes()), headers={"content-type": "audio/wav"})
    result = asyncio.run(routes.create_analysis(upload, USER))
    assert result["status"] == "completed"
    assert collection.docs[result["analysis_id"]]["result_json"]["audio"]["duration_seconds"] == .1


@pytest.mark.parametrize("name,content,expected", [("beat.exe", b"x", 415), ("beat.wav", b"broken", 422)])
def test_upload_rejects_invalid_files(monkeypatch, tmp_path, name, content, expected):
    monkeypatch.setattr(routes.settings, "media_root", tmp_path)
    monkeypatch.setattr(routes, "beat_analyses_collection", Collection())
    async def user_id(_): return UID
    monkeypatch.setattr(routes, "get_user_id", user_id)
    monkeypatch.setattr(routes, "analyze_beat", lambda *_: (_ for _ in ()).throw(ValueError("bad audio")))
    upload = UploadFile(filename=name, file=io.BytesIO(content), headers={"content-type": "audio/wav" if name.endswith("wav") else "application/octet-stream"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(routes.create_analysis(upload, USER))
    assert exc.value.status_code == expected


def test_upload_enforces_streamed_size_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(routes.settings, "media_root", tmp_path)
    monkeypatch.setattr(routes.settings, "beat_analysis_max_upload_size", 8)
    upload = UploadFile(filename="x.wav", file=io.BytesIO(b"0123456789"), headers={"content-type": "audio/wav"})
    with pytest.raises(HTTPException) as exc: asyncio.run(routes.create_analysis(upload, USER))
    assert exc.value.status_code == 413


def test_get_owner_guard_discard_and_missing(monkeypatch, tmp_path):
    analysis_id = str(uuid4())
    doc = {"_id": analysis_id, "user_id": UID, "status": "completed", "progress": 100, "expires_at": datetime.now(timezone.utc) + timedelta(hours=1), "result_json": {}}
    collection = Collection({analysis_id: doc})
    monkeypatch.setattr(routes, "beat_analyses_collection", collection)
    monkeypatch.setattr(routes, "_root", lambda: tmp_path)
    async def user_id(_): return UID
    monkeypatch.setattr(routes, "get_user_id", user_id)
    assert asyncio.run(routes.get_analysis(analysis_id, USER))["status"] == "completed"
    with pytest.raises(HTTPException) as missing: asyncio.run(routes.get_analysis(str(uuid4()), USER))
    assert missing.value.status_code == 404
    asyncio.run(routes.discard_analysis(analysis_id, USER))
    assert doc["status"] == "discarded"


def test_expired_and_cleanup(monkeypatch, tmp_path):
    analysis_id = str(uuid4())
    doc = {"_id": analysis_id, "user_id": UID, "status": "completed", "progress": 100, "expires_at": datetime.now(timezone.utc) - timedelta(hours=1), "result_json": {}}
    collection = Collection({analysis_id: doc})
    monkeypatch.setattr(routes, "beat_analyses_collection", collection)
    monkeypatch.setattr(routes, "_root", lambda: tmp_path)
    async def user_id(_): return UID
    monkeypatch.setattr(routes, "get_user_id", user_id)
    assert asyncio.run(routes.get_analysis(analysis_id, USER))["status"] == "expired"
    doc["status"] = "completed"
    assert asyncio.run(routes.cleanup_expired_beat_analyses()) == 1
    assert doc["status"] == "expired"


def test_ffmpeg_missing_fails_cleanly(monkeypatch, tmp_path):
    from services.beat_analysis import analyzer
    monkeypatch.setattr(analyzer.shutil, "which", lambda _: None)
    with pytest.raises(RuntimeError): analyzer.convert_to_analysis_wav(tmp_path / "x.wav", tmp_path / "y.wav")


def test_publish_uses_existing_temp_audio(monkeypatch, tmp_path):
    analysis_id = str(uuid4())
    source = tmp_path / "original.wav"
    source.write_bytes(b"audio")
    doc = {"_id": analysis_id, "user_id": UID, "status": "completed", "expires_at": datetime.now(timezone.utc) + timedelta(hours=1), "temporary_original_path": str(source), "original_extension": "wav"}
    analyses = Collection({analysis_id: doc})
    posts = Collection()
    monkeypatch.setattr(routes, "beat_analyses_collection", analyses)
    monkeypatch.setattr(routes, "post_collection", posts)
    async def user_id(_): return UID
    monkeypatch.setattr(routes, "get_user_id", user_id)
    class Batch:
        database_fields = {"audio_key": "beats/id/audio.wav", "audio_format": "wav"}
        replacements = [SimpleNamespace(final_key="")]
        def apply(self): pass
        def finalize(self): pass
        def rollback(self): pass
    monkeypatch.setattr(routes, "stage_file", lambda *args: "temp/staged.wav")
    monkeypatch.setattr(routes, "MediaBatch", lambda *args: Batch())
    monkeypatch.setattr(routes, "MediaReplacement", lambda *args: object())
    monkeypatch.setattr(routes, "delete_post_directory", lambda *args: None)
    monkeypatch.setattr(routes, "with_post_media_urls", lambda post: post)
    result_id = "507f1f77bcf86cd799439099"
    async def insert(doc):
        posts.docs[result_id] = {**doc, "_id": result_id}
        return SimpleNamespace(inserted_id=result_id)
    posts.insert_one = insert
    async def find_post(q): return posts.docs[result_id]
    posts.find_one = find_post
    async def update_post(q, update): posts.docs[result_id].update(update["$set"]); return SimpleNamespace(matched_count=1)
    posts.update_one = update_post
    payload = routes.PublishRequest(analysis_id=analysis_id, title="Test")
    published = asyncio.run(routes.publish_from_analysis(payload, USER))
    assert published.title == "Test"
    assert doc["status"] == "published"
