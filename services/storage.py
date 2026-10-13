from __future__ import annotations

import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from uuid import uuid4

from fastapi import UploadFile

from config.settings import settings
from services.media_validation import validate_image_upload
from services.beat_audio import ProcessedBeatAudio, prepare_uploaded_audio


class StorageError(RuntimeError):
    pass


class StorageProvider(ABC):
    @abstractmethod
    def save(self, key: str, data: bytes) -> str: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def delete_prefix(self, prefix: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def read(self, key: str) -> bytes: ...

    @abstractmethod
    def open(self, key: str, mode: str = "rb") -> BinaryIO: ...

    @abstractmethod
    def get_public_url(self, key: str) -> str: ...

    @abstractmethod
    def generate_key(self, namespace: str, entity_id: str, filename: str) -> str: ...

    @abstractmethod
    def replace_from_temp(self, temp_key: str, final_key: str) -> str | None: ...

    @abstractmethod
    def rollback_replace(self, final_key: str, backup_key: str | None) -> None: ...

    @abstractmethod
    def health_check(self) -> bool: ...


class LocalStorageProvider(StorageProvider):
    def __init__(self, root: Path, base_url: str):
        self.root = root
        self.base_url = base_url.rstrip("/")

    @staticmethod
    def _normalize_key(key: str) -> str:
        raw_key = key.replace("\\", "/")
        normalized = str(PurePosixPath(raw_key))
        path = PurePosixPath(normalized)
        if (
            not raw_key
            or normalized in {"", "."}
            or path.is_absolute()
            or ".." in path.parts
            or "\x00" in normalized
        ):
            raise StorageError("Invalid storage key")
        return normalized

    @staticmethod
    def _safe_segment(value: str) -> str:
        if not value or value in {".", ".."} or any(character in value for character in "/\\\x00"):
            raise StorageError("Invalid storage key segment")
        return value

    def _path(self, key: str) -> Path:
        normalized = self._normalize_key(key)
        root = self.root.resolve()
        candidate = (root / normalized).resolve()
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise StorageError("Storage key escapes media root") from exc
        return candidate

    def save(self, key: str, data: bytes) -> str:
        normalized = self._normalize_key(key)
        target = self._path(normalized)
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_directory = self._path("temp")
        temp_directory.mkdir(parents=True, exist_ok=True)
        temporary_path = temp_directory / f"write-{uuid4().hex}.tmp"
        try:
            with temporary_path.open("wb") as output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return normalized

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
        parent = path.parent
        root = self.root.resolve()
        while parent != root and parent.name != "temp":
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def delete_prefix(self, prefix: str) -> None:
        path = self._path(prefix)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def open(self, key: str, mode: str = "rb") -> BinaryIO:
        if mode not in {"rb", "r"}:
            raise StorageError("Storage files can only be opened for reading")
        return self._path(key).open(mode)

    def get_public_url(self, key: str) -> str:
        return f"{self.base_url}/{self._normalize_key(key)}"

    def generate_key(self, namespace: str, entity_id: str, filename: str) -> str:
        return "/".join(
            (
                self._safe_segment(namespace),
                self._safe_segment(entity_id),
                self._safe_segment(filename),
            )
        )

    def replace_from_temp(self, temp_key: str, final_key: str) -> str | None:
        source = self._path(temp_key)
        target = self._path(final_key)
        if not source.is_file():
            raise StorageError("Staged media does not exist")
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_key = None
        if target.exists():
            backup_key = f"temp/backups/{uuid4().hex}.bak"
            backup = self._path(backup_key)
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(target, backup)
        try:
            os.replace(source, target)
        except Exception:
            if backup_key:
                os.replace(self._path(backup_key), target)
            raise
        return backup_key

    def rollback_replace(self, final_key: str, backup_key: str | None) -> None:
        self.delete(final_key)
        if backup_key and self.exists(backup_key):
            target = self._path(final_key)
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(self._path(backup_key), target)

    def health_check(self) -> bool:
        try:
            temp_directory = self._path("temp")
            temp_directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=temp_directory) as probe:
                probe.write(b"ok")
                probe.seek(0)
                return probe.read() == b"ok"
        except OSError:
            return False


def get_storage_provider() -> StorageProvider:
    if settings.storage_provider == "local":
        return LocalStorageProvider(settings.media_root, settings.media_base_url)
    raise StorageError(f"Unsupported storage provider: {settings.storage_provider}")


storage = get_storage_provider()


@dataclass(slots=True)
class MediaReplacement:
    final_key: str
    temp_key: str
    provider: StorageProvider
    backup_key: str | None = None
    applied: bool = False

    def apply(self) -> None:
        self.backup_key = self.provider.replace_from_temp(self.temp_key, self.final_key)
        self.applied = True

    def rollback(self) -> None:
        if self.applied:
            self.provider.rollback_replace(self.final_key, self.backup_key)
            self.applied = False
            self.backup_key = None
        else:
            self.provider.delete(self.temp_key)

    def finalize(self) -> None:
        self.provider.delete(self.temp_key)
        if self.backup_key:
            self.provider.delete(self.backup_key)


@dataclass(slots=True)
class MediaBatch:
    replacements: list[MediaReplacement]
    database_fields: dict[str, str]
    old_keys: set[str] = field(default_factory=set)
    private_audio: ProcessedBeatAudio | None = None
    private_backup: Path | None = None
    private_applied: bool = False
    old_original_key: str | None = None

    def apply(self) -> None:
        applied: list[MediaReplacement] = []
        try:
            for replacement in self.replacements:
                replacement.apply()
                applied.append(replacement)
            if self.private_audio:
                target = settings.beats_original_dir / self.private_audio.master_key
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                target.parent.chmod(0o700)
                if target.exists():
                    self.private_backup = target.with_name(f".{target.name}.{uuid4().hex}.bak")
                    os.replace(target, self.private_backup)
                try:
                    os.replace(self.private_audio.staged_original, target)
                except Exception:
                    if self.private_backup and self.private_backup.exists():
                        os.replace(self.private_backup, target)
                        self.private_backup = None
                    raise
                self.private_applied = True
        except Exception:
            for replacement in reversed(applied):
                replacement.rollback()
            for replacement in self.replacements[len(applied):]:
                replacement.rollback()
            raise

    def rollback(self) -> None:
        for replacement in reversed(self.replacements):
            replacement.rollback()
        if self.private_audio:
            target = settings.beats_original_dir / self.private_audio.master_key
            if self.private_applied:
                target.unlink(missing_ok=True)
                if self.private_backup and self.private_backup.exists():
                    os.replace(self.private_backup, target)
            shutil.rmtree(self.private_audio.staged_original.parent, ignore_errors=True)
            self.private_applied = False

    def finalize(self) -> None:
        final_keys = {replacement.final_key for replacement in self.replacements}
        for replacement in self.replacements:
            replacement.finalize()
        for old_key in self.old_keys - final_keys:
            self.replacements[0].provider.delete(old_key)
        if self.private_audio:
            if self.private_backup:
                self.private_backup.unlink(missing_ok=True)
            shutil.rmtree(self.private_audio.staged_original.parent, ignore_errors=True)
            if self.old_original_key and self.old_original_key != self.private_audio.master_key:
                _delete_private_original(self.old_original_key)


def _stage(data: bytes, extension: str, provider: StorageProvider = storage) -> str:
    temp_key = f"temp/{uuid4().hex}.{extension}"
    provider.save(temp_key, data)
    return temp_key


def stage_file(source: str | Path, extension: str, provider: StorageProvider = storage) -> str:
    """Copy a file into provider staging without reading it into memory."""
    temp_key = f"temp/{uuid4().hex}.{extension}"
    if not isinstance(provider, LocalStorageProvider):
        raise StorageError("File staging is not supported by this storage provider")
    target = provider._path(temp_key)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return temp_key


async def prepare_avatar(
    user_id: str,
    upload: UploadFile,
    old_key: str | None = None,
    provider: StorageProvider = storage,
) -> MediaBatch:
    image = await validate_image_upload(upload)
    final_key = provider.generate_key("avatars", user_id, f"avatar-{uuid4().hex}.webp")
    replacement = MediaReplacement(final_key, _stage(image.data, image.extension, provider), provider)
    return MediaBatch(
        [replacement],
        {"avatar_key": final_key},
        {old_key} if old_key else set(),
    )


async def prepare_beat_media(
    post_id: str,
    cover_file: UploadFile | None,
    audio_file: UploadFile | None,
    *,
    producer_id: str | None = None,
    old_cover_key: str | None = None,
    old_audio_key: str | None = None,
    old_original_key: str | None = None,
    preview_start: float | None = None,
    preview_end: float | None = None,
    provider: StorageProvider = storage,
) -> MediaBatch:
    cover = await validate_image_upload(cover_file) if cover_file is not None else None
    audio = await prepare_uploaded_audio(
        audio_file, producer_id or post_id, preview_start, preview_end
    ) if audio_file is not None else None
    replacements: list[MediaReplacement] = []
    fields: dict[str, str] = {}

    try:
        if cover:
            cover_key = provider.generate_key("covers", post_id, "cover.webp")
            replacements.append(MediaReplacement(cover_key, _stage(cover.data, cover.extension, provider), provider))
            fields.update({"cover_key": cover_key, "cover_format": "webp"})
        if audio:
            preview_root = settings.beats_preview_dir.resolve()
            media_root = settings.media_root.resolve()
            try:
                preview_prefix = preview_root.relative_to(media_root).as_posix()
            except ValueError as exc:
                raise RuntimeError("BEATS_PREVIEW_DIR must be within MEDIA_ROOT") from exc
            preview_key = provider.generate_key(preview_prefix, post_id, f"preview.{audio.preview_format}")
            replacements.append(MediaReplacement(preview_key, _stage(audio.staged_preview.read_bytes(), audio.preview_format, provider), provider))
            fields.update({"audio_key": preview_key, **audio.database_fields, "preview_key": preview_key})
    except Exception:
        for replacement in replacements:
            provider.delete(replacement.temp_key)
        if audio:
            shutil.rmtree(audio.staged_original.parent, ignore_errors=True)
        raise

    old_keys = set()
    if cover and old_cover_key:
        old_keys.add(old_cover_key)
    if audio and old_audio_key:
        old_keys.add(old_audio_key)
    return MediaBatch(replacements, fields, old_keys, private_audio=audio, old_original_key=old_original_key)


def create_user_directories(user_id: str) -> str:
    return f"avatars/{user_id}"


def delete_user_directories(
    user_id: str,
    post_ids: list[str] | None = None,
    original_keys: list[str] | None = None,
    preview_keys: list[str] | None = None,
) -> None:
    storage.delete_prefix(f"avatars/{user_id}")
    for post_id in post_ids or []:
        storage.delete_prefix(f"covers/{post_id}")
        storage.delete_prefix(f"previews/{post_id}")
        storage.delete_prefix(f"beats/{post_id}")
    for key in original_keys or []:
        _delete_private_original(key)
    for key in preview_keys or []:
        storage.delete(key)


def _delete_private_original(key: str) -> None:
    normalized = PurePosixPath(key)
    if normalized.is_absolute() or ".." in normalized.parts:
        raise StorageError("Invalid private original key")
    target = (settings.beats_original_dir / Path(*normalized.parts)).resolve()
    try:
        target.relative_to(settings.beats_original_dir.resolve())
    except ValueError as exc:
        raise StorageError("Private original path escapes storage root") from exc
    target.unlink(missing_ok=True)


def delete_post_directory(
    user_id: str,
    post_id: str,
    master_key: str | None = None,
    preview_key: str | None = None,
) -> None:
    storage.delete_prefix(f"covers/{post_id}")
    storage.delete_prefix(f"previews/{post_id}")
    storage.delete_prefix(f"beats/{post_id}")
    if preview_key:
        storage.delete(preview_key)
    if master_key:
        _delete_private_original(master_key)


def media_url(key: str | None) -> str | None:
    return storage.get_public_url(key) if key else None


def profile_media_url(user: dict) -> str | None:
    avatar_key = user.get("avatar_key")
    return media_url(str(avatar_key)) if avatar_key else None


def with_post_media_urls(post: dict) -> dict:
    payload = dict(post)
    for private_field in (
        "master_key", "original_storage_key", "original_path", "temporary_original_path", "temporary_analysis_path",
        "original_sha256", "original_filename", "original_size", "original_audio_format", "mime_type",
        "preview_mime_type", "preview_size", "preview_storage_key", "processing_error",
        "publication_status", "rights_confirmed", "rights_confirmation_version", "rights_confirmed_at",
        "analysis_id",
    ):
        payload.pop(private_field, None)
    post_id = str(payload.get("_id", ""))
    cover_key = payload.get("cover_key")
    media_status = payload.get("processing_status", "failed")
    preview_key = payload.get("preview_key")
    cover_url = media_url(cover_key)
    preview_url = media_url(preview_key) if media_status == "ready" and preview_key else None
    payload["cover_key"] = cover_key
    payload.pop("audio_key", None)
    payload.pop("preview_key", None)
    payload["cover_image_url"] = cover_url
    payload["caratula"] = cover_url
    payload["media_status"] = media_status
    payload["preview_url"] = preview_url
    payload["audio_url"] = preview_url
    return payload


def storage_ready() -> bool:
    return storage.health_check()
