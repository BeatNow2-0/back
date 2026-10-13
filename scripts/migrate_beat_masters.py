"""Migrate legacy public beat audio to private masters and public previews.

Dry-run unless --execute is supplied. A legacy source is removed only after
verifying the private SHA-256, public preview existence, and preview decoding.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from fastapi import UploadFile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.db import post_collection  # noqa: E402
from config.settings import settings  # noqa: E402
from services.beat_audio import probe_audio_file  # noqa: E402
from services.storage import LocalStorageProvider, prepare_beat_media, storage  # noqa: E402


def _private_path(key: str) -> Path | None:
    candidate = (settings.beats_original_dir / key).resolve()
    try:
        candidate.relative_to(settings.beats_original_dir.resolve())
    except ValueError:
        return None
    return candidate


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_preview(key: str, extension: str) -> None:
    if not storage.exists(key):
        raise RuntimeError("preview_missing")
    if isinstance(storage, LocalStorageProvider):
        probe_audio_file(storage._path(key), extension)
        return
    with tempfile.NamedTemporaryFile(suffix=f".{extension}") as target:
        with storage.open(key) as source:
            while chunk := source.read(1024 * 1024):
                target.write(chunk)
        target.flush()
        probe_audio_file(Path(target.name), extension)


def _manifest_entry(post: dict) -> dict:
    fields = (
        "audio_key", "audio_format", "cover_key", "master_key", "preview_key",
        "original_storage_key", "preview_storage_key", "publication_status",
        "processing_status", "processing_error", "original_sha256",
    )
    return {"beat_id": str(post["_id"]), "before": {field: post.get(field) for field in fields}}


def _append_manifest(path: Path, entry: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as output:
        output.write(json.dumps(entry, sort_keys=True) + "\n")


async def _mark_failed(post: dict, reason: str, execute: bool, manifest: Path) -> None:
    print(json.dumps({"beat_id": str(post["_id"]), "status": "failed", "reason": reason}, sort_keys=True))
    if not execute:
        return
    _append_manifest(manifest, _manifest_entry(post))
    await post_collection.update_one(
        {"_id": post["_id"]},
        {"$set": {
            "publication_status": "failed", "processing_status": "failed",
            "processing_error": f"migration_{reason}",
            "migration_checked_at": datetime.now(timezone.utc),
        }, "$unset": {"preview_key": ""}},
    )


async def _migrate_cover(post: dict, execute: bool, manifest: Path) -> None:
    beat_id = str(post["_id"])
    cover_key = post.get("cover_key")
    if cover_key and cover_key.startswith("covers/") and storage.exists(cover_key):
        return
    if not cover_key and post.get("cover_format") not in {None, "pending"}:
        cover_key = storage.generate_key("beats", beat_id, f"cover.{post['cover_format']}")
    if not cover_key or not storage.exists(cover_key):
        return
    extension = Path(cover_key).suffix.lower().lstrip(".") or "webp"
    new_key = storage.generate_key("covers", beat_id, f"cover.{extension}")
    print(json.dumps({"beat_id": beat_id, "status": "would_migrate_cover" if not execute else "migrating_cover", "source": cover_key}, sort_keys=True))
    if not execute:
        return
    _append_manifest(manifest, _manifest_entry(post))
    data = storage.read(cover_key)
    expected_hash = hashlib.sha256(data).hexdigest()
    storage.save(new_key, data)
    if not storage.exists(new_key) or hashlib.sha256(storage.read(new_key)).hexdigest() != expected_hash:
        storage.delete(new_key)
        raise RuntimeError("cover_verification_failed")
    result = await post_collection.update_one({"_id": post["_id"]}, {"$set": {"cover_key": new_key}})
    if result.matched_count == 0:
        storage.delete(new_key)
        raise RuntimeError("beat_disappeared_during_cover_migration")
    if cover_key != new_key:
        storage.delete(cover_key)
    post["cover_key"] = new_key


async def migrate(execute: bool, post_id: str | None, manifest: Path) -> None:
    query = {"_id": ObjectId(post_id)} if post_id else {}
    async for post in post_collection.find(query):
        beat_id = str(post["_id"])
        try:
            await _migrate_cover(post, execute, manifest)
        except Exception as exc:
            await _mark_failed(post, f"cover_{type(exc).__name__}", execute, manifest)
            continue
        master_key = post.get("master_key") or post.get("original_storage_key")
        preview_key = post.get("preview_key") or post.get("preview_storage_key")
        master_path = _private_path(master_key) if master_key else None

        if master_path and master_path.is_file() and preview_key and storage.exists(preview_key):
            legacy_public_key = post.get("audio_key")
            try:
                expected_hash = post.get("original_sha256")
                actual_hash = _sha256(master_path)
                if expected_hash and actual_hash != expected_hash:
                    raise RuntimeError("master_hash_mismatch")
                _verify_preview(preview_key, Path(preview_key).suffix.lower().lstrip("."))
            except Exception as exc:
                await _mark_failed(post, str(exc), execute, manifest)
                continue
            if execute:
                _append_manifest(manifest, _manifest_entry(post))
                await post_collection.update_one(
                    {"_id": post["_id"]},
                    {"$set": {
                        "master_key": master_key, "preview_key": preview_key,
                        "audio_key": preview_key, "original_sha256": actual_hash,
                        "processing_status": "ready", "publication_status": "published",
                        "migration_checked_at": datetime.now(timezone.utc),
                    }, "$unset": {"processing_error": "", "original_storage_key": "", "preview_storage_key": ""}},
                )
                if legacy_public_key and legacy_public_key != preview_key and storage.exists(legacy_public_key):
                    try:
                        storage.delete(legacy_public_key)
                    except Exception:
                        await _mark_failed(post, "legacy_cleanup_failed", True, manifest)
                        continue
            print(json.dumps({"beat_id": beat_id, "status": "verified" if execute else "would_verify"}, sort_keys=True))
            continue

        # audio_key is a legacy master only if it was not identified as a preview
        # by an earlier partial migration.
        audio_key = None if preview_key else post.get("audio_key")
        if not audio_key and not preview_key and post.get("audio_format") not in {None, "pending"}:
            audio_key = storage.generate_key("beats", beat_id, f"audio.{post['audio_format']}")
        if not audio_key or not storage.exists(audio_key):
            await _mark_failed(post, "source_missing", execute, manifest)
            continue
        suffix = Path(audio_key).suffix.lower().lstrip(".")
        if suffix not in {"wav", "mp3", "flac", "m4a"}:
            await _mark_failed(post, "unsupported_format", execute, manifest)
            continue
        if not execute:
            print(json.dumps({"beat_id": beat_id, "status": "would_migrate", "source": audio_key}, sort_keys=True))
            continue

        _append_manifest(manifest, _manifest_entry(post))
        with storage.open(audio_key) as source:
            upload = UploadFile(filename=f"original.{suffix}", file=source)
            try:
                batch = await prepare_beat_media(
                    beat_id, None, upload, producer_id=str(post.get("user_id", "")),
                    old_audio_key=audio_key, old_original_key=master_key,
                )
            except Exception as exc:
                await upload.close()
                await _mark_failed(post, type(exc).__name__, True, manifest)
                continue
            try:
                batch.apply()
                new_master_key = batch.database_fields["master_key"]
                new_preview_key = batch.database_fields["preview_key"]
                new_master_path = _private_path(new_master_key)
                if not new_master_path or not new_master_path.is_file():
                    raise RuntimeError("master_missing_after_copy")
                if _sha256(new_master_path) != batch.database_fields["original_sha256"]:
                    raise RuntimeError("master_hash_mismatch")
                _verify_preview(new_preview_key, batch.database_fields["audio_format"])
                result = await post_collection.update_one(
                    {"_id": post["_id"]},
                    {"$set": {
                        **batch.database_fields, "processing_status": "ready",
                        "publication_status": "published",
                        "migration_checked_at": datetime.now(timezone.utc),
                    }, "$unset": {"processing_error": "", "original_storage_key": "", "preview_storage_key": ""}},
                )
                if result.matched_count == 0:
                    raise RuntimeError("beat_disappeared")
                batch.finalize()
            except Exception as exc:
                batch.rollback()
                await _mark_failed(post, type(exc).__name__, True, manifest)
                continue
            finally:
                await upload.close()
        print(json.dumps({"beat_id": beat_id, "status": "migrated", "master_sha256": batch.database_fields["original_sha256"]}, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate public BeatNow audio to private masters plus previews")
    parser.add_argument("--execute", action="store_true", help="Write files and database updates; default is dry-run")
    parser.add_argument("--post-id", help="Migrate one beat only")
    parser.add_argument("--manifest", type=Path, default=Path("preview-migration-manifest.jsonl"), help="Append-only rollback/audit manifest")
    args = parser.parse_args()
    if args.post_id and not ObjectId.is_valid(args.post_id):
        parser.error("--post-id must be a valid Mongo ObjectId")
    asyncio.run(migrate(args.execute, args.post_id, args.manifest.resolve()))


if __name__ == "__main__":
    main()
