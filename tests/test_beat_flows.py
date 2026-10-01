import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import HTTPException

from model.user_shemas import CurrentUser
from routes import posts_routes


POST_ID = "507f1f77bcf86cd799439099"
OWNER_ID = "507f1f77bcf86cd799439011"


def _post():
    return {
        "_id": ObjectId(POST_ID),
        "user_id": OWNER_ID,
        "publication_date": datetime.now(timezone.utc),
        "title": "Original",
        "description": None,
        "tags": [],
        "genre": None,
        "moods": [],
        "instruments": [],
        "bpm": 90,
        "likes": 0,
        "saves": 0,
        "views": 0,
        "cover_format": "webp",
        "audio_format": "mp3",
        "cover_key": f"beats/{POST_ID}/cover.webp",
        "audio_key": f"beats/{POST_ID}/audio.mp3",
    }


def _user(user_id=OWNER_ID):
    return CurrentUser(
        _id=ObjectId(user_id),
        username="owner",
        email="owner@example.com",
        password="hashed",
        is_active=True,
    )


class FakePosts:
    def __init__(self, document, fail_update=False):
        self.document = document
        self.fail_update = fail_update

    async def find_one(self, query):
        return dict(self.document) if self.document else None

    async def update_one(self, query, update):
        if self.fail_update:
            raise RuntimeError("simulated Mongo failure")
        self.document.update(update.get("$set", {}))
        return SimpleNamespace(matched_count=1)

    async def delete_one(self, query):
        self.document = None
        return SimpleNamespace(deleted_count=1)


class FakeBatch:
    def __init__(self):
        self.database_fields = {
            "cover_key": f"beats/{POST_ID}/cover.webp",
            "cover_format": "webp",
            "audio_key": f"beats/{POST_ID}/audio.wav",
            "audio_format": "wav",
        }
        self.applied = False
        self.rolled_back = False
        self.finalized = False

    def apply(self):
        self.applied = True

    def rollback(self):
        self.rolled_back = True

    def finalize(self):
        self.finalized = True


class CreatePosts:
    def __init__(self):
        self.document = None

    async def insert_one(self, document):
        self.document = {**document, "_id": ObjectId(POST_ID)}
        return SimpleNamespace(inserted_id=ObjectId(POST_ID))

    async def update_one(self, query, update):
        self.document.update(update["$set"])
        return SimpleNamespace(matched_count=1)

    async def find_one(self, query):
        return dict(self.document) if self.document else None

    async def delete_one(self, query):
        self.document = None
        return SimpleNamespace(deleted_count=1)


def _update_call(current_user):
    return posts_routes.update_post(
        POST_ID,
        cover_file=object(),
        audio_file=object(),
        title="Updated",
        description=None,
        genre=None,
        tags=None,
        moods=None,
        instruments=None,
        bpm=120,
        current_user=current_user,
    )


def _upload_call(current_user):
    return posts_routes.upload_post(
        cover_file=object(),
        audio_file=object(),
        title="New beat",
        description="Description",
        genre="Hip hop",
        tags="one,two",
        moods="focused",
        instruments="drums",
        bpm=100,
        current_user=current_user,
    )


def test_beat_create_attaches_storage_keys(monkeypatch):
    collection = CreatePosts()
    batch = FakeBatch()

    async def user_id(_username):
        return OWNER_ID

    async def prepare(*args, **kwargs):
        return batch

    monkeypatch.setattr(posts_routes, "post_collection", collection)
    monkeypatch.setattr(posts_routes, "get_user_id", user_id)
    monkeypatch.setattr(posts_routes, "prepare_beat_media", prepare)

    created = asyncio.run(_upload_call(_user()))
    assert batch.applied and batch.finalized
    assert created.cover_key == f"beats/{POST_ID}/cover.webp"
    assert created.audio_key == f"beats/{POST_ID}/audio.wav"


def test_beat_create_rolls_back_document_when_media_fails(monkeypatch):
    collection = CreatePosts()
    batch = FakeBatch()
    deleted_directories = []

    async def user_id(_username):
        return OWNER_ID

    async def prepare(*args, **kwargs):
        return batch

    def fail_apply():
        raise RuntimeError("simulated media failure")

    batch.apply = fail_apply
    monkeypatch.setattr(posts_routes, "post_collection", collection)
    monkeypatch.setattr(posts_routes, "get_user_id", user_id)
    monkeypatch.setattr(posts_routes, "prepare_beat_media", prepare)
    monkeypatch.setattr(posts_routes, "delete_post_directory", lambda *args: deleted_directories.append(args))

    with pytest.raises(RuntimeError):
        asyncio.run(_upload_call(_user()))
    assert batch.rolled_back
    assert collection.document is None
    assert deleted_directories == [(OWNER_ID, POST_ID)]


def test_beat_update_commits_media_and_generates_new_urls(monkeypatch):
    collection = FakePosts(_post())
    batch = FakeBatch()

    async def user_id(_username):
        return OWNER_ID

    async def prepare(*args, **kwargs):
        return batch

    monkeypatch.setattr(posts_routes, "post_collection", collection)
    monkeypatch.setattr(posts_routes, "get_user_id", user_id)
    monkeypatch.setattr(posts_routes, "prepare_beat_media", prepare)

    updated = asyncio.run(_update_call(_user()))
    assert batch.applied and batch.finalized and not batch.rolled_back
    assert updated.title == "Updated"
    assert updated.cover_image_url == f"https://res.beatnow.app/beats/{POST_ID}/cover.webp"
    assert updated.audio_url == f"https://res.beatnow.app/beats/{POST_ID}/audio.wav"


def test_beat_update_rolls_back_media_when_mongo_fails(monkeypatch):
    collection = FakePosts(_post(), fail_update=True)
    batch = FakeBatch()

    async def user_id(_username):
        return OWNER_ID

    async def prepare(*args, **kwargs):
        return batch

    monkeypatch.setattr(posts_routes, "post_collection", collection)
    monkeypatch.setattr(posts_routes, "get_user_id", user_id)
    monkeypatch.setattr(posts_routes, "prepare_beat_media", prepare)

    with pytest.raises(RuntimeError):
        asyncio.run(_update_call(_user()))
    assert batch.applied and batch.rolled_back and not batch.finalized


def test_other_user_cannot_update_or_delete_beat(monkeypatch):
    collection = FakePosts(_post())

    async def other_user_id(_username):
        return "507f1f77bcf86cd799439012"

    monkeypatch.setattr(posts_routes, "post_collection", collection)
    monkeypatch.setattr(posts_routes, "get_user_id", other_user_id)

    with pytest.raises(HTTPException) as update_error:
        asyncio.run(_update_call(_user("507f1f77bcf86cd799439012")))
    assert update_error.value.status_code == 403

    with pytest.raises(HTTPException) as delete_error:
        asyncio.run(posts_routes.delete_publication(POST_ID, _user("507f1f77bcf86cd799439012"), None))
    assert delete_error.value.status_code == 403


def test_beat_delete_removes_relations_and_media(monkeypatch):
    collection = FakePosts(_post())
    calls = []

    class Interactions:
        async def delete_many(self, query):
            calls.append(("interactions", query))

    class Lyrics:
        async def update_many(self, query, update):
            calls.append(("lyrics", query))

    async def user_id(_username):
        return OWNER_ID

    monkeypatch.setattr(posts_routes, "post_collection", collection)
    monkeypatch.setattr(posts_routes, "interactions_collection", Interactions())
    monkeypatch.setattr(posts_routes, "lyrics_collection", Lyrics())
    monkeypatch.setattr(posts_routes, "get_user_id", user_id)
    monkeypatch.setattr(posts_routes, "delete_post_directory", lambda *args: calls.append(("storage", args)))

    asyncio.run(posts_routes.delete_publication(POST_ID, _user(), None))
    assert collection.document is None
    assert [call[0] for call in calls] == ["interactions", "lyrics", "storage"]


def test_post_id_invalid_is_422_and_valid_missing_is_404(monkeypatch):
    collection = FakePosts(None)
    monkeypatch.setattr(posts_routes, "post_collection", collection)

    with pytest.raises(HTTPException) as invalid:
        asyncio.run(posts_routes.read_publication("not-an-id", _user(), None))
    assert invalid.value.status_code == 422

    with pytest.raises(HTTPException) as missing:
        asyncio.run(posts_routes.read_publication(POST_ID, _user(), None))
    assert missing.value.status_code == 404
