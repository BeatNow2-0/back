import asyncio
from pathlib import Path
from types import SimpleNamespace

from bson import ObjectId

from model.user_shemas import CurrentUser
from routes import users_routes
from services import storage as storage_module
from services.storage import LocalStorageProvider


USER_ID = "507f1f77bcf86cd799439011"


class FakeUsers:
    def __init__(self, doc):
        self.doc = dict(doc)

    async def find_one(self, query, projection=None):
        return dict(self.doc) if str(self.doc["_id"]) == str(query.get("_id")) else None

    async def update_one(self, query, update):
        self.doc.update(update.get("$set", {}))
        for key in update.get("$unset", {}):
            self.doc.pop(key, None)
        return SimpleNamespace(matched_count=1)


def _user(avatar_key=None):
    return CurrentUser(
        _id=ObjectId(USER_ID), username="user", email="user@example.com", password="hashed",
        is_active=True, avatar_key=avatar_key,
    )


def test_change_photo_persists_new_key_then_removes_old_and_returns_new_url(tmp_path, monkeypatch):
    provider = LocalStorageProvider(Path(tmp_path) / "media", "https://res.beatnow.app")
    old_key = f"avatars/{USER_ID}/avatar-old.webp"
    provider.save(old_key, b"old")
    collection = FakeUsers({"_id": ObjectId(USER_ID), "avatar_key": old_key})
    monkeypatch.setattr(users_routes, "users_collection", collection)
    monkeypatch.setattr(users_routes, "storage", provider)
    monkeypatch.setattr(storage_module, "storage", provider)
    async def user_id(_username):
        return USER_ID
    async def valid_image(_upload):
        return SimpleNamespace(data=b"new", extension="webp")
    monkeypatch.setattr(users_routes, "get_user_id", user_id)
    monkeypatch.setattr("services.storage.validate_image_upload", valid_image)
    async def prepare(user_id, upload, old_key=None):
        return await storage_module.prepare_avatar(user_id, upload, old_key, provider=provider)
    monkeypatch.setattr(users_routes, "prepare_avatar", prepare)

    response = asyncio.run(users_routes.change_photo_profile(_user(old_key), object()))
    new_key = collection.doc["avatar_key"]
    assert new_key != old_key and new_key.startswith(f"avatars/{USER_ID}/avatar-")
    assert not provider.exists(old_key)
    assert provider.read(new_key) == b"new"
    assert response["profile_image_url"] == provider.get_public_url(new_key)
    assert response["photo_profile"] == response["profile_image_url"]
    assert list((Path(tmp_path) / "media" / "temp").iterdir()) == []


def test_change_photo_rolls_back_when_database_update_fails(tmp_path, monkeypatch):
    provider = LocalStorageProvider(Path(tmp_path) / "media", "https://res.beatnow.app")
    old_key = f"avatars/{USER_ID}/avatar-old.webp"
    provider.save(old_key, b"old")
    collection = FakeUsers({"_id": ObjectId(USER_ID), "avatar_key": old_key})
    async def fail_update(_query, _update):
        raise RuntimeError("simulated Mongo failure")
    collection.update_one = fail_update
    monkeypatch.setattr(users_routes, "users_collection", collection)
    monkeypatch.setattr(users_routes, "storage", provider)
    monkeypatch.setattr(storage_module, "storage", provider)
    async def user_id(_username):
        return USER_ID
    async def valid_image(_upload):
        return SimpleNamespace(data=b"new", extension="webp")
    monkeypatch.setattr(users_routes, "get_user_id", user_id)
    monkeypatch.setattr("services.storage.validate_image_upload", valid_image)
    async def prepare(user_id, upload, old_key=None):
        return await storage_module.prepare_avatar(user_id, upload, old_key, provider=provider)
    monkeypatch.setattr(users_routes, "prepare_avatar", prepare)

    try:
        asyncio.run(users_routes.change_photo_profile(_user(old_key), object()))
        assert False, "expected Mongo failure"
    except RuntimeError as exc:
        assert str(exc) == "simulated Mongo failure"
    assert provider.read(old_key) == b"old"
    remaining_avatars = list((Path(tmp_path) / "media" / "avatars" / USER_ID).glob("avatar-*"))
    assert [path.name for path in remaining_avatars] == ["avatar-old.webp"]
    assert list((Path(tmp_path) / "media" / "temp").iterdir()) == []


def test_profile_read_endpoints_use_current_avatar_key(tmp_path, monkeypatch):
    key = f"avatars/{USER_ID}/avatar-current.webp"
    provider = LocalStorageProvider(Path(tmp_path) / "media", "https://res.beatnow.app")
    collection = FakeUsers({"_id": ObjectId(USER_ID), "avatar_key": key, "username": "user", "email": "user@example.com", "is_active": True})
    monkeypatch.setattr(users_routes, "users_collection", collection)
    monkeypatch.setattr(users_routes, "storage", provider)

    class EmptyCollection:
        async def count_documents(self, _query):
            return 0
        async def find_one(self, _query):
            return None

    monkeypatch.setattr(users_routes, "follows_collection", EmptyCollection())
    monkeypatch.setattr(users_routes, "post_collection", EmptyCollection())
    async def user_id(_username):
        return USER_ID
    monkeypatch.setattr(users_routes, "get_user_id", user_id)

    me = asyncio.run(users_routes.read_users_me(_user(key)))
    profile = asyncio.run(users_routes.get_user_profile(USER_ID, _user(key)))
    expected = provider.get_public_url(key)
    assert me.profile_image_url == expected and me.photo_profile == expected
    assert profile.profile_image_url == expected and profile.photo_profile == expected


def test_delete_photo_uses_stored_versioned_key(tmp_path, monkeypatch):
    provider = LocalStorageProvider(Path(tmp_path) / "media", "https://res.beatnow.app")
    key = f"avatars/{USER_ID}/avatar-version-abc.webp"
    provider.save(key, b"avatar")
    collection = FakeUsers({"_id": ObjectId(USER_ID), "avatar_key": key})
    monkeypatch.setattr(users_routes, "users_collection", collection)
    monkeypatch.setattr(users_routes, "storage", provider)
    async def user_id(_username):
        return USER_ID
    monkeypatch.setattr(users_routes, "get_user_id", user_id)

    response = asyncio.run(users_routes.delete_photo_profile(_user(key)))
    assert response["profile_image_url"] is None
    assert not provider.exists(key)
    assert "avatar_key" not in collection.doc
