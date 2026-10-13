from __future__ import annotations

import shutil
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, Request
from bson import ObjectId
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from config.db import beat_analyses_collection, post_collection
from config.security import get_current_user, get_user_id
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from model.post_shemas import NewPost, PostInDB
from model.user_shemas import CurrentUser
from services.beat_analysis.analyzer import analyze_beat
from services.beat_audio import probe_audio_file
from services.storage import delete_post_directory, prepare_beat_media, with_post_media_urls

router = APIRouter()
logger = logging.getLogger(__name__)
ALLOWED = {"wav", "mp3", "flac", "m4a"}


def _root() -> Path:
    # Keep unpublished originals outside the directory exposed by the media server.
    return settings.media_root.parent / ".beat-analysis-tmp"


def _remove(doc: dict) -> None:
    shutil.rmtree(_root() / str(doc["_id"]), ignore_errors=True)


def _public(doc: dict) -> dict:
    return {"analysis_id": str(doc["_id"]), "status": doc["status"], "progress": doc.get("progress", 100), "analysis": doc.get("result_json"), "error": doc.get("error"), "expires_at": doc["expires_at"]}


@router.post("", status_code=201)
async def create_analysis(request: Request, file: UploadFile = File(...), current_user: CurrentUser = Depends(get_current_user)):
    await enforce_rate_limit(request, f"beat-upload:{current_user.id}", settings.beat_upload_rate_limit)
    raw_filename = file.filename or "audio"
    if "/" in raw_filename or "\\" in raw_filename:
        raise HTTPException(415, "Invalid audio filename")
    filename = Path(raw_filename).name
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
    record_created = False
    try:
        with original.open("wb") as output:
            while chunk := await file.read(1024 * 1024):
                total += len(chunk)
                if total > min(settings.beat_analysis_max_upload_size, settings.max_audio_upload_size):
                    raise HTTPException(413, "Audio file too large")
                output.write(chunk)
        if not total:
            raise HTTPException(415, "Empty audio upload")
        user_id = await get_user_id(current_user.username)
        record = {"_id": analysis_id, "user_id": user_id, "original_filename": filename, "original_extension": ext, "temporary_original_path": str(original), "temporary_analysis_path": str(folder / "analysis.wav"), "status": "processing", "progress": 10, "analysis_version": "1.0", "created_at": datetime.now(timezone.utc), "expires_at": datetime.now(timezone.utc) + timedelta(hours=settings.beat_analysis_ttl_hours)}
        await beat_analyses_collection.insert_one(record)
        record_created = True
        await run_in_threadpool(probe_audio_file, original, ext)
        result = await run_in_threadpool(analyze_beat, original, folder / "analysis.wav")
        result["analysis_version"] = "1.0"
        await beat_analyses_collection.update_one({"_id": analysis_id}, {"$set": {"status": "completed", "progress": 100, "result_json": result, "completed_at": datetime.now(timezone.utc)}})
        record.update(status="completed", progress=100, result_json=result)
        return {"analysis_id": str(analysis_id), "status": "completed", "expires_at": record["expires_at"], "analysis_version": "1.0", **result}
    except HTTPException:
        _remove({"_id": analysis_id})
        if record_created:
            await beat_analyses_collection.update_one(
                {"_id": analysis_id},
                {"$set": {"status": "failed", "progress": 0, "error": "Audio could not be analyzed"}},
            )
        raise
    except RuntimeError as exc:
        if str(exc) in {"FFmpeg is required to analyze this audio", "FFprobe is required to validate audio duration"}:
            logger.exception("Beat analysis dependency unavailable")
            await beat_analyses_collection.update_one({"_id": analysis_id}, {"$set": {"status": "failed", "error": "Audio analysis is temporarily unavailable"}})
            _remove({"_id": analysis_id})
            raise HTTPException(503, "Audio analysis is temporarily unavailable") from exc
        logger.exception("Beat analysis failed for upload %s", filename)
        await beat_analyses_collection.update_one({"_id": analysis_id}, {"$set": {"status": "failed", "error": "Audio could not be analyzed"}})
        _remove({"_id": analysis_id})
        raise HTTPException(422, "Audio could not be analyzed") from exc
    except Exception as exc:
        logger.exception("Beat analysis failed for upload %s", filename)
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
    rights_confirmed: bool = False
    preview_start: float | None = None
    preview_end: float | None = None


async def publish_from_analysis(payload: PublishRequest, current_user: CurrentUser):
    if not payload.rights_confirmed:
        raise HTTPException(422, "Rights confirmation is required")
    doc = await _owned(payload.analysis_id, current_user)
    if doc["status"] != "completed" or doc["expires_at"] <= datetime.now(timezone.utc):
        raise HTTPException(409, "Analysis is not available for publishing")
    source = Path(doc["temporary_original_path"])
    if not source.is_file():
        raise HTTPException(410, "Temporary audio is no longer available")
    claim = await beat_analyses_collection.update_one({"_id": doc["_id"], "status": "completed"}, {"$set": {"status": "publishing"}})
    if claim.matched_count == 0:
        raise HTTPException(409, "Analysis is already being published")
    user_id = doc["user_id"]
    new_id = ObjectId()
    post_id = str(new_id)
    audio_upload = UploadFile(filename=doc["original_filename"], file=source.open("rb"))
    batch = None
    result = None
    try:
        post = NewPost(**payload.model_dump(exclude={"analysis_id", "preview_start", "preview_end"}))
        batch = await prepare_beat_media(
            post_id,
            None,
            audio_upload,
            producer_id=user_id,
            preview_start=payload.preview_start,
            preview_end=payload.preview_end,
        )
        batch.apply()
        result = await post_collection.insert_one({"_id": new_id, "user_id": user_id, "publication_date": datetime.now(timezone.utc), "likes": 0, "saves": 0, "views": 0, **post.model_dump(), "audio_format": settings.preview_format, "cover_format": "pending", "publication_status": "published", "processing_status": "ready", "rights_confirmed": True, "rights_confirmation_version": settings.rights_confirmation_version, "rights_confirmed_at": datetime.now(timezone.utc), "analysis_id": str(doc["_id"]), **batch.database_fields})
        await beat_analyses_collection.update_one({"_id": doc["_id"], "status": "publishing"}, {"$set": {"status": "published", "published_post_id": post_id}})
        batch.finalize()
        _remove(doc)
    except Exception:
        if batch:
            batch.rollback()
        if result is not None:
            await post_collection.delete_one({"_id": new_id})
            delete_post_directory(user_id, post_id, batch.database_fields.get("master_key") if batch else None, batch.database_fields.get("preview_key") if batch else None)
        await beat_analyses_collection.update_one({"_id": doc["_id"], "status": "publishing"}, {"$set": {"status": "completed"}})
        raise
    finally:
        await audio_upload.close()
    saved = await post_collection.find_one({"_id": new_id})
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
