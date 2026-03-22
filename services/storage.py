from __future__ import annotations

import shutil
from pathlib import Path

from fastapi import HTTPException, UploadFile, status

from config.settings import settings

ALLOWED_IMAGE_CONTENT_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif"}
ALLOWED_AUDIO_CONTENT_TYPES = {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024
MAX_AUDIO_SIZE = 20 * 1024 * 1024


def _safe_user_path(user_id: str) -> Path:
    return settings.media_root / user_id


def create_user_directories(user_id: str) -> Path:
    base = _safe_user_path(user_id)
    (base / "photo_profile").mkdir(parents=True, exist_ok=True)
    (base / "posts").mkdir(parents=True, exist_ok=True)
    if settings.default_profile_image.exists():
        target = base / "photo_profile" / f"photo_profile{settings.default_profile_image.suffix.lower()}"
        if not target.exists():
            shutil.copy2(settings.default_profile_image, target)
    return base


def delete_user_directories(user_id: str) -> None:
    base = _safe_user_path(user_id)
    if base.exists():
        shutil.rmtree(base)


async def _validate_upload(upload: UploadFile, allowed: dict[str, str], max_bytes: int, label: str) -> str:
    if upload.content_type not in allowed:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Unsupported {label} content type")
    contents = await upload.read(max_bytes + 1)
    if len(contents) > max_bytes:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, detail=f"{label.capitalize()} too large")
    await upload.seek(0)
    return allowed[upload.content_type]


async def save_post_files(user_id: str, post_id: str, cover_file: UploadFile, audio_file: UploadFile) -> tuple[str, str]:
    image_suffix = await _validate_upload(cover_file, ALLOWED_IMAGE_CONTENT_TYPES, MAX_IMAGE_SIZE, "image")
    audio_suffix = await _validate_upload(audio_file, ALLOWED_AUDIO_CONTENT_TYPES, MAX_AUDIO_SIZE, "audio")

    post_dir = _safe_user_path(user_id) / "posts" / post_id
    post_dir.mkdir(parents=True, exist_ok=True)

    cover_path = post_dir / f"cover{image_suffix}"
    audio_path = post_dir / f"audio{audio_suffix}"

    with cover_path.open("wb") as image_buffer:
        shutil.copyfileobj(cover_file.file, image_buffer)
    with audio_path.open("wb") as audio_buffer:
        shutil.copyfileobj(audio_file.file, audio_buffer)

    return image_suffix.lstrip("."), audio_suffix.lstrip(".")


async def update_post_files(user_id: str, post_id: str, cover_file: UploadFile | None, audio_file: UploadFile | None) -> dict[str, str]:
    post_dir = _safe_user_path(user_id) / "posts" / post_id
    post_dir.mkdir(parents=True, exist_ok=True)
    updated: dict[str, str] = {}

    if cover_file is not None:
        image_suffix = await _validate_upload(cover_file, ALLOWED_IMAGE_CONTENT_TYPES, MAX_IMAGE_SIZE, "image")
        for existing in post_dir.glob("cover.*"):
            existing.unlink(missing_ok=True)
        with (post_dir / f"cover{image_suffix}").open("wb") as buffer:
            shutil.copyfileobj(cover_file.file, buffer)
        updated["cover_format"] = image_suffix.lstrip(".")

    if audio_file is not None:
        audio_suffix = await _validate_upload(audio_file, ALLOWED_AUDIO_CONTENT_TYPES, MAX_AUDIO_SIZE, "audio")
        for existing in post_dir.glob("audio.*"):
            existing.unlink(missing_ok=True)
        with (post_dir / f"audio{audio_suffix}").open("wb") as buffer:
            shutil.copyfileobj(audio_file.file, buffer)
        updated["audio_format"] = audio_suffix.lstrip(".")

    return updated


def delete_post_directory(user_id: str, post_id: str) -> None:
    post_dir = _safe_user_path(user_id) / "posts" / post_id
    if post_dir.exists():
        shutil.rmtree(post_dir)
