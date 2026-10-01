import asyncio
from datetime import datetime, timezone

from bson import ObjectId
from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from model.user_shemas import CurrentUser
from routes import interactions_routes


POST_ID = "507f1f77bcf86cd799439099"


class AtomicInteractions:
    def __init__(self, initial=None):
        self.document = initial or None
        self.lock = asyncio.Lock()

    async def find_one_and_update(self, query, update, upsert=False, return_document=None):
        async with self.lock:
            field = "like_date" if "like_date" in update.get("$set", {}) or "like_date" in update.get("$unset", {}) else "saved_date"
            if "$set" in update:
                if self.document and self.document.get(field) is not None:
                    raise DuplicateKeyError("duplicate transition")
                self.document = dict(self.document or {})
                self.document.update(update["$set"])
                return dict(self.document)
            if not self.document or self.document.get(field) is None:
                return None
            previous = dict(self.document)
            self.document.pop(field, None)
            return previous

    async def update_one(self, query, update):
        if self.document and "$unset" in update:
            for field in update["$unset"]:
                self.document.pop(field, None)


class AtomicPosts:
    def __init__(self, likes=0, saves=0):
        self.document = {"_id": ObjectId(POST_ID), "likes": likes, "saves": saves}
        self.lock = asyncio.Lock()

    async def find_one_and_update(self, query, update, return_document=None):
        async with self.lock:
            field, amount = next(iter(update["$inc"].items()))
            if amount < 0 and self.document.get(field, 0) <= 0:
                return None
            self.document[field] = self.document.get(field, 0) + amount
            return dict(self.document)


def _user():
    return CurrentUser(
        _id=ObjectId("507f1f77bcf86cd799439011"),
        username="beta_user",
        email="beta@example.com",
        password="hashed",
        is_active=True,
    )


async def _run_concurrently(factory):
    return await asyncio.gather(factory(), factory())


def test_concurrent_like_only_increments_once(monkeypatch):
    interactions = AtomicInteractions()
    posts = AtomicPosts()

    async def exists(_post_id, db=None):
        return None

    async def user_id(_username):
        return "507f1f77bcf86cd799439011"

    monkeypatch.setattr(interactions_routes, "interactions_collection", interactions)
    monkeypatch.setattr(interactions_routes, "post_collection", posts)
    monkeypatch.setattr(interactions_routes, "check_post_exists", exists)
    monkeypatch.setattr(interactions_routes, "get_user_id", user_id)

    async def attempt():
        try:
            await interactions_routes._add_transition(POST_ID, "like_date", _user())
            return 200
        except HTTPException as exc:
            return exc.status_code

    assert sorted(asyncio.run(_run_concurrently(attempt))) == [200, 400]
    assert posts.document["likes"] == 1


def test_concurrent_unsave_never_makes_counter_negative(monkeypatch):
    interactions = AtomicInteractions(
        {
            "user_id": "507f1f77bcf86cd799439011",
            "post_id": POST_ID,
            "saved_date": datetime.now(timezone.utc),
        }
    )
    posts = AtomicPosts(saves=1)

    async def exists(_post_id, db=None):
        return None

    async def user_id(_username):
        return "507f1f77bcf86cd799439011"

    monkeypatch.setattr(interactions_routes, "interactions_collection", interactions)
    monkeypatch.setattr(interactions_routes, "post_collection", posts)
    monkeypatch.setattr(interactions_routes, "check_post_exists", exists)
    monkeypatch.setattr(interactions_routes, "get_user_id", user_id)

    async def attempt():
        try:
            await interactions_routes._remove_transition(POST_ID, "saved_date", _user())
            return 200
        except HTTPException as exc:
            return exc.status_code

    assert sorted(asyncio.run(_run_concurrently(attempt))) == [200, 400]
    assert posts.document["saves"] == 0
