from __future__ import annotations

import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config.db import beat_analyses_collection, post_collection
from config.security import get_current_user, get_user_id
from config.settings import settings
from model.post_shemas import NewPost, PostInDB
from model.user_shemas import CurrentUser
from services.beat_analysis.analyzer import analyze_beat
from services.storage import MediaBatch, MediaReplacement, delete_post_directory, stage_file, storage, with_post_media_urls

router = APIRouter()
ALLOWED = {"wav", "mp3", "flac", "m4a"}


def _root() -> Path:
    # Keep unpublished originals outside the directory exposed by the media server.
    return settings.media_root.parent / ".beat-analysis-tmp"


def _remove(doc: dict) -> None:
    shutil.rmtree(_root() / str(doc["_id"]), ignore_errors=True)


def _public(doc: dict) -> dict:
    return {"analysis_id": str(doc["_id"]), "status": doc["status"], "progress": doc.get("progress", 100), "analysis": doc.get("result_json"), "error": doc.get("error"), "expires_at": doc["expires_at"]}


@router.post("", status_code=201)
async def create_analysis(file: UploadFile = File(...), current_user: CurrentUser = Depends(get_current_user)):
    filename = Path(file.filename or "audio").name
    ext = Path(filename).suffix.lower().lstrip(".")
    if ext not in ALLOWED:
        raise HTTPException(415, "Unsupported audio format")
    if file.content_type and file.content_type not in {"audio/wav", "audio/x-wav", "audio/mpeg", "audio/flac", "audio/x-flac", "audio/mp4", "audio/x-m4a", "application/octet-stream"}:
        raise HTTPException(415, "Unsupported audio MIME type")
    analysis_id = str(uuid4())
    folder = _root() / str(analysis_id)
    folder.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.parent.chmod(0o700)
    folder.mkdir(parents=True, exist_ok=False)
    original = folder / f"original.{ext}"
    total = 0
    try:
        with original.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > settings.beat_analysis_max_upload_size:
                    raise HTTPException(413, "Audio file too large")
                output.write(chunk)
        if not total:
            raise HTTPException(415, "Empty audio upload")
        user_id = await get_user_id(current_user.username)
        record = {"_id": analysis_id, "user_id": user_id, "original_filename": filename, "original_extension": ext, "temporary_original_path": str(original), "temporary_analysis_path": str(folder / "analysis.wav"), "status": "processing", "progress": 10, "analysis_version": "1.0", "created_at": datetime.now(timezone.utc), "expires_at": datetime.now(timezone.utc) + timedelta(hours=settings.beat_analysis_ttl_hours)}
        await beat_analyses_collection.insert_one(record)
        result = await run_in_threadpool(analyze_beat, original, folder / "analysis.wav")
        result["analysis_version"] = "1.0"
        await beat_analyses_collection.update_one({"_id": analysis_id}, {"$set": {"status": "completed", "progress": 100, "result_json": result, "completed_at": datetime.now(timezone.utc)}})
        record.update(status="completed", progress=100, result_json=result)
        return {"analysis_id": str(analysis_id), "status": "completed", "expires_at": record["expires_at"], "analysis_version": "1.0", **result}
    except HTTPException:
        _remove({"_id": analysis_id})
        raise
    except Exception as exc:
        await beat_analyses_collection.update_one({"_id": analysis_id}, {"$set": {"status": "failed", "error": "Audio could not be analyzed"}})
        _remove({"_id": analysis_id})
        raise HTTPException(422, "Audio could not be analyzed") from exc
    finally:
        await file.close()


async def _owned(analysis_id: str, user: CurrentUser) -> dict:
    try:
        oid = str(UUID(analysis_id))
    except Exception as exc:
        raise HTTPException(404, "Analysis not found") from exc
    user_id = await get_user_id(user.username)
    doc = await beat_analyses_collection.find_one({"_id": oid, "user_id": user_id})
    if not doc:
        raise HTTPException(404, "Analysis not found")
    return doc


@router.get("/{analysis_id}")
async def get_analysis(analysis_id: str, current_user: CurrentUser = Depends(get_current_user)):
    doc = await _owned(analysis_id, current_user)
    if doc["status"] not in {"published", "discarded", "expired"} and doc["expires_at"] <= datetime.now(timezone.utc):
        await beat_analyses_collection.update_one({"_id": doc["_id"]}, {"$set": {"status": "expired"}})
        _remove(doc)
        doc["status"] = "expired"
    return _public(doc)


@router.delete("/{analysis_id}", status_code=204)
async def discard_analysis(analysis_id: str, current_user: CurrentUser = Depends(get_current_user)):
    doc = await _owned(analysis_id, current_user)
    if doc["status"] != "published":
        _remove(doc)
        await beat_analyses_collection.update_one({"_id": doc["_id"]}, {"$set": {"status": "discarded", "progress": 0}})


class PublishRequest(NewPost):
    analysis_id: str
    preview_start: float | None = None
    preview_end: float | None = None


async def publish_from_analysis(payload: PublishRequest, current_user: CurrentUser):
    doc = await _owned(payload.analysis_id, current_user)
    if doc["status"] != "completed" or doc["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(409, "Analysis is not available for publishing")
    source = Path(doc["temporary_original_path"])
    if not source.is_file():
        raise HTTPException(410, "Temporary audio is no longer available")
    audio_key = storage.generate_key("beats", "pending", f"audio.{doc['original_extension']}")
    staged = stage_file(source, doc["original_extension"])
    claim = await beat_analyses_collection.update_one({"_id": doc["_id"], "status": "completed"}, {"$set": {"status": "publishing"}})
    if claim.matched_count == 0:
        storage.delete(staged)
        raise HTTPException(409, "Analysis is already being published")
    batch = MediaBatch([MediaReplacement(audio_key, staged, storage)], {"audio_key": audio_key, "audio_format": doc["original_extension"]})
    result = None
    try:
        post = NewPost(**payload.model_dump(exclude={"analysis_id", "preview_start", "preview_end"}))
        user_id = doc["user_id"]
        result = await post_collection.insert_one({"user_id": user_id, "publication_date": datetime.now(timezone.utc), "likes": 0, "saves": 0, "views": 0, **post.model_dump(), "audio_format": "pending", "cover_format": "pending", "analysis_id": str(doc["_id"])})
        post_id = str(result.inserted_id)
        audio_key = storage.generate_key("beats", post_id, f"audio.{doc['original_extension']}")
        batch.replacements[0].final_key = audio_key
        batch.apply()
        await post_collection.update_one({"_id": result.inserted_id}, {"$set": batch.database_fields})
        batch.finalize()
        await beat_analyses_collection.update_one({"_id": doc["_id"], "status": "publishing"}, {"$set": {"status": "published", "published_post_id": post_id}})
        _remove(doc)
    except Exception:
        batch.rollback()
        if result is not None:
            await post_collection.delete_one({"_id": result.inserted_id})
            delete_post_directory(user_id, post_id)
        await beat_analyses_collection.update_one({"_id": doc["_id"], "status": "publishing"}, {"$set": {"status": "completed"}})
        raise
    saved = await post_collection.find_one({"_id": result.inserted_id})
    return PostInDB(**with_post_media_urls(saved))


publish_router = APIRouter()
@publish_router.post("/from-analysis", response_model=PostInDB)
async def publish_route(payload: PublishRequest, current_user: CurrentUser = Depends(get_current_user)):
    return await publish_from_analysis(payload, current_user)


async def cleanup_expired_beat_analyses() -> int:
    now = datetime.now(timezone.utc)
    cursor = beat_analyses_collection.find({"status": {"$nin": ["published", "discarded", "expired"]}, "expires_at": {"$lte": now}})
    count = 0
    async for doc in cursor:
        _remove(doc)
        await beat_analyses_collection.update_one({"_id": doc["_id"], "status": {"$ne": "published"}}, {"$set": {"status": "expired", "progress": 0}})
        count += 1
    return count
