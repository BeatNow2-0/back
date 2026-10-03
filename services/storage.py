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
from services.media_validation import validate_audio_upload, validate_image_upload


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

    def apply(self) -> None:
        applied: list[MediaReplacement] = []
        try:
            for replacement in self.replacements:
                replacement.apply()
                applied.append(replacement)
        except Exception:
            for replacement in reversed(applied):
                replacement.rollback()
            for replacement in self.replacements[len(applied):]:
                replacement.rollback()
            raise

    def rollback(self) -> None:
        for replacement in reversed(self.replacements):
            replacement.rollback()

    def finalize(self) -> None:
        final_keys = {replacement.final_key for replacement in self.replacements}
        for replacement in self.replacements:
            replacement.finalize()
        for old_key in self.old_keys - final_keys:
            self.replacements[0].provider.delete(old_key)


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
    old_cover_key: str | None = None,
    old_audio_key: str | None = None,
    provider: StorageProvider = storage,
) -> MediaBatch:
    cover = await validate_image_upload(cover_file) if cover_file is not None else None
    audio = await validate_audio_upload(audio_file) if audio_file is not None else None
    replacements: list[MediaReplacement] = []
    fields: dict[str, str] = {}

    if cover:
        cover_key = provider.generate_key("beats", post_id, "cover.webp")
        replacements.append(MediaReplacement(cover_key, _stage(cover.data, cover.extension, provider), provider))
        fields.update({"cover_key": cover_key, "cover_format": "webp"})
    if audio:
        audio_key = provider.generate_key("beats", post_id, f"audio.{audio.extension}")
        replacements.append(MediaReplacement(audio_key, _stage(audio.data, audio.extension, provider), provider))
        fields.update({"audio_key": audio_key, "audio_format": audio.extension})

    old_keys = set()
    if cover and old_cover_key:
        old_keys.add(old_cover_key)
    if audio and old_audio_key:
        old_keys.add(old_audio_key)
    return MediaBatch(replacements, fields, old_keys)


def create_user_directories(user_id: str) -> str:
    return f"avatars/{user_id}"


def delete_user_directories(user_id: str, post_ids: list[str] | None = None) -> None:
    storage.delete_prefix(f"avatars/{user_id}")
    for post_id in post_ids or []:
        storage.delete_prefix(f"beats/{post_id}")


def delete_post_directory(user_id: str, post_id: str) -> None:
    storage.delete_prefix(f"beats/{post_id}")


def media_url(key: str | None) -> str | None:
    return storage.get_public_url(key) if key else None


def profile_media_url(user: dict) -> str | None:
    avatar_key = user.get("avatar_key")
    return media_url(str(avatar_key)) if avatar_key else None


def with_post_media_urls(post: dict) -> dict:
    payload = dict(post)
    post_id = str(payload.get("_id", ""))
    cover_key = payload.get("cover_key")
    audio_key = payload.get("audio_key")
    if post_id and not cover_key and payload.get("cover_format") not in {None, "pending"}:
        cover_key = storage.generate_key("beats", post_id, f"cover.{payload['cover_format']}")
    if post_id and not audio_key and payload.get("audio_format") not in {None, "pending"}:
        audio_key = storage.generate_key("beats", post_id, f"audio.{payload['audio_format']}")
    cover_url = media_url(cover_key)
    audio_url = media_url(audio_key)
    payload["cover_key"] = cover_key
    payload["audio_key"] = audio_key
    payload["cover_image_url"] = cover_url
    payload["caratula"] = cover_url
    payload["audio_url"] = audio_url
    return payload


def storage_ready() -> bool:
    return storage.health_check()
