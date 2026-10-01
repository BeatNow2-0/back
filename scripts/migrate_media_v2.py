from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from bson import ObjectId
from fastapi import UploadFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.db import post_collection, users_collection  # noqa: E402
from services.storage import prepare_avatar, prepare_beat_media, storage  # noqa: E402


def _first_file(directory: Path, stems: tuple[str, ...]) -> Path | None:
    if not directory.is_dir():
        return None
    for stem in stems:
        exact = directory / stem
        if exact.is_file():
            return exact
        matches = sorted(path for path in directory.glob(f"{stem}.*") if path.is_file())
        if matches:
            return matches[0]
    return None


def _avatar_source(legacy_root: Path, user_id: str) -> Path | None:
    user_root = legacy_root / user_id
    return _first_file(user_root, ("photo_profile",)) or _first_file(
        user_root / "photo_profile", ("photo_profile",)
    )


def _post_source(legacy_root: Path, user_id: str, post_id: str, stems: tuple[str, ...]) -> Path | None:
    return _first_file(legacy_root / user_id / "posts" / post_id, stems)


def _upload(path: Path | None) -> UploadFile | None:
    if path is None:
        return None
    return UploadFile(filename=path.name, file=path.open("rb"))


def _report(kind: str, entity_id: str, status: str, **details) -> None:
    print(json.dumps({"kind": kind, "id": entity_id, "status": status, **details}, sort_keys=True))


async def _migrate_avatar(user: dict, legacy_root: Path, execute: bool) -> None:
    user_id = str(user["_id"])
    current_key = user.get("avatar_key")
    if current_key and storage.exists(current_key):
        _report("avatar", user_id, "already_migrated", key=current_key)
        return
    source = _avatar_source(legacy_root, user_id)
    if source is None:
        _report("avatar", user_id, "source_missing")
        return
    expected_key = storage.generate_key("avatars", user_id, "avatar.webp")
    if not execute:
        _report("avatar", user_id, "would_migrate", source=str(source), key=expected_key)
        return

    upload = _upload(source)
    batch = await prepare_avatar(user_id, upload, current_key)
    try:
        batch.apply()
        if not all(storage.exists(item.final_key) for item in batch.replacements):
            raise RuntimeError("Avatar verification failed after copy")
        result = await users_collection.update_one(
            {"_id": user["_id"]},
            {"$set": batch.database_fields},
        )
        if result.matched_count == 0:
            raise RuntimeError("User no longer exists")
    except Exception:
        batch.rollback()
        raise
    finally:
        await upload.close()
    batch.finalize()
    _report("avatar", user_id, "migrated", source=str(source), key=expected_key)


async def _migrate_post(post: dict, legacy_root: Path, execute: bool) -> None:
    post_id = str(post["_id"])
    user_id = str(post["user_id"])
    cover_complete = bool(post.get("cover_key") and storage.exists(post["cover_key"]))
    audio_complete = bool(post.get("audio_key") and storage.exists(post["audio_key"]))
    if cover_complete and audio_complete:
        _report("beat", post_id, "already_migrated")
        return

    cover_source = None if cover_complete else _post_source(legacy_root, user_id, post_id, ("cover", "caratula"))
    audio_source = None if audio_complete else _post_source(legacy_root, user_id, post_id, ("audio",))
    if cover_source is None and audio_source is None:
        _report("beat", post_id, "source_missing", user_id=user_id)
        return
    if not execute:
        _report(
            "beat",
            post_id,
            "would_migrate",
            user_id=user_id,
            cover_source=str(cover_source) if cover_source else None,
            audio_source=str(audio_source) if audio_source else None,
        )
        return

    cover_upload = _upload(cover_source)
    audio_upload = _upload(audio_source)
    batch = await prepare_beat_media(
        post_id,
        cover_upload,
        audio_upload,
        old_cover_key=post.get("cover_key"),
        old_audio_key=post.get("audio_key"),
    )
    try:
        batch.apply()
        if not all(storage.exists(item.final_key) for item in batch.replacements):
            raise RuntimeError("Beat media verification failed after copy")
        result = await post_collection.update_one(
            {"_id": post["_id"]},
            {"$set": batch.database_fields},
        )
        if result.matched_count == 0:
            raise RuntimeError("Post no longer exists")
    except Exception:
        batch.rollback()
        raise
    finally:
        if cover_upload:
            await cover_upload.close()
        if audio_upload:
            await audio_upload.close()
    batch.finalize()
    _report("beat", post_id, "migrated", user_id=user_id, **batch.database_fields)


async def migrate(legacy_root: Path, execute: bool, user_id: str | None) -> None:
    if not legacy_root.is_dir():
        raise SystemExit("Legacy media root does not exist or is not a directory")
    if user_id and not ObjectId.is_valid(user_id):
        raise SystemExit("--user-id must be a valid MongoDB ObjectId")
    user_query = {"_id": ObjectId(user_id)} if user_id else {}
    async for user in users_collection.find(user_query):
        try:
            await _migrate_avatar(user, legacy_root, execute)
        except Exception as exc:
            _report("avatar", str(user["_id"]), "failed", error=type(exc).__name__)

    post_query = {"user_id": user_id} if user_id else {}
    async for post in post_collection.find(post_query):
        try:
            await _migrate_post(post, legacy_root, execute)
        except Exception as exc:
            _report("beat", str(post["_id"]), "failed", error=type(exc).__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Migrate legacy BeatNow media without deleting source files")
    parser.add_argument(
        "--legacy-root",
        type=Path,
        default=Path(os.environ["LEGACY_MEDIA_ROOT"]) if os.getenv("LEGACY_MEDIA_ROOT") else None,
        help="Legacy media root (or set LEGACY_MEDIA_ROOT)",
    )
    parser.add_argument("--execute", action="store_true", help="Apply copies and MongoDB updates; default is dry-run")
    parser.add_argument("--user-id", help="Optionally migrate one MongoDB user id")
    args = parser.parse_args()
    if args.legacy_root is None:
        parser.error("--legacy-root or LEGACY_MEDIA_ROOT is required")
    return args


if __name__ == "__main__":
    arguments = parse_args()
    asyncio.run(migrate(arguments.legacy_root.resolve(), arguments.execute, arguments.user_id))
