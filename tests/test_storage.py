from pathlib import Path

import pytest

from services.storage import LocalStorageProvider, MediaBatch, MediaReplacement, StorageError, prepare_avatar, with_post_media_urls


def _provider(tmp_path: Path) -> LocalStorageProvider:
    return LocalStorageProvider(tmp_path / "media", "https://res.beatnow.app/")


def test_storage_save_exists_read_delete_and_url(tmp_path):
    provider = _provider(tmp_path)
    key = provider.generate_key("beats", "abc123", "audio.mp3")
    provider.save(key, b"audio")
    assert provider.exists(key)
    assert provider.read(key) == b"audio"
    assert provider.get_public_url(key) == "https://res.beatnow.app/beats/abc123/audio.mp3"
    provider.delete(key)
    assert not provider.exists(key)


def test_storage_health_probe_leaves_no_file(tmp_path):
    provider = _provider(tmp_path)
    assert provider.health_check() is True
    assert list((tmp_path / "media" / "temp").iterdir()) == []


@pytest.mark.parametrize("key", ["", ".", "../secret", "/absolute", "beats/../../secret", "beats\\..\\secret"])
def test_storage_rejects_path_traversal(tmp_path, key):
    provider = _provider(tmp_path)
    with pytest.raises(StorageError):
        provider.save(key, b"unsafe")


def test_replacement_rollback_restores_old_file(tmp_path):
    provider = _provider(tmp_path)
    final_key = "beats/abc123/cover.webp"
    temp_key = "temp/new-cover.webp"
    provider.save(final_key, b"old")
    provider.save(temp_key, b"new")
    replacement = MediaReplacement(final_key, temp_key, provider)

    replacement.apply()
    assert provider.read(final_key) == b"new"
    replacement.rollback()
    assert provider.read(final_key) == b"old"


def test_replacement_finalize_keeps_new_file(tmp_path):
    provider = _provider(tmp_path)
    final_key = "avatars/user123/avatar.webp"
    temp_key = "temp/new-avatar.webp"
    provider.save(final_key, b"old")
    provider.save(temp_key, b"new")
    replacement = MediaReplacement(final_key, temp_key, provider)

    replacement.apply()
    replacement.finalize()
    assert provider.read(final_key) == b"new"
    assert replacement.backup_key is not None
    assert not provider.exists(replacement.backup_key)


def test_batch_failure_restores_previously_replaced_file(tmp_path, monkeypatch):
    provider = _provider(tmp_path)
    provider.save("beats/abc123/cover.webp", b"old-cover")
    provider.save("beats/abc123/audio.wav", b"old-audio")
    provider.save("temp/new-cover.webp", b"new-cover")
    provider.save("temp/new-audio.wav", b"new-audio")
    cover = MediaReplacement("beats/abc123/cover.webp", "temp/new-cover.webp", provider)
    audio = MediaReplacement("beats/abc123/audio.wav", "temp/new-audio.wav", provider)
    original_replace = provider.replace_from_temp

    def fail_audio(temp_key, final_key):
        if final_key.endswith("audio.wav"):
            raise StorageError("simulated audio failure")
        return original_replace(temp_key, final_key)

    monkeypatch.setattr(provider, "replace_from_temp", fail_audio)
    batch = MediaBatch([cover, audio], {})
    with pytest.raises(StorageError):
        batch.apply()
    batch.rollback()
    assert provider.read("beats/abc123/cover.webp") == b"old-cover"
    assert provider.read("beats/abc123/audio.wav") == b"old-audio"


def test_new_media_urls_use_resource_domain():
    payload = with_post_media_urls(
        {
            "_id": "beat123",
            "user_id": "user123",
            "cover_key": "beats/beat123/cover.webp",
            "audio_key": "beats/beat123/audio.mp3",
        }
    )
    assert payload["cover_image_url"] == "https://res.beatnow.app/beats/beat123/cover.webp"
    assert payload["caratula"] == payload["cover_image_url"]
    assert payload["audio_url"] == "https://res.beatnow.app/beats/beat123/audio.mp3"


def test_prepare_avatar_generates_a_unique_versioned_key(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import services.storage as storage_module

    provider = _provider(tmp_path)

    async def valid_image(_upload):
        return SimpleNamespace(data=b"webp-image", extension="webp")

    monkeypatch.setattr(storage_module, "validate_image_upload", valid_image)
    first = asyncio.run(prepare_avatar("user123", object(), provider=provider))
    second = asyncio.run(prepare_avatar("user123", object(), provider=provider))

    first_key = first.database_fields["avatar_key"]
    second_key = second.database_fields["avatar_key"]
    assert first_key.startswith("avatars/user123/avatar-") and first_key.endswith(".webp")
    assert second_key != first_key
    assert provider.exists(first.replacements[0].temp_key)
    assert provider.exists(second.replacements[0].temp_key)


def test_avatar_batch_removes_old_key_after_finalize_and_rolls_back_on_failure(tmp_path):
    provider = _provider(tmp_path)
    old_key = "avatars/user123/avatar-old.webp"
    provider.save(old_key, b"old-avatar")
    provider.save("temp/new-avatar.webp", b"new-avatar")
    new_key = "avatars/user123/avatar-new.webp"
    batch = MediaBatch([MediaReplacement(new_key, "temp/new-avatar.webp", provider)], {"avatar_key": new_key}, {old_key})

    batch.apply()
    batch.rollback()
    assert provider.read(old_key) == b"old-avatar"
    assert not provider.exists(new_key)
    assert not provider.exists("temp/new-avatar.webp")

    provider.save("temp/new-avatar.webp", b"new-avatar")
    batch = MediaBatch([MediaReplacement(new_key, "temp/new-avatar.webp", provider)], {"avatar_key": new_key}, {old_key})
    batch.apply()
    batch.finalize()
    assert not provider.exists(old_key)
    assert provider.read(new_key) == b"new-avatar"
    assert not provider.exists("temp/new-avatar.webp")
