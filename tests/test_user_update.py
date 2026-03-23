import asyncio

from bson import ObjectId
from fastapi import HTTPException

from model.user_shemas import CurrentUser, UserUpdate
from routes import users_routes


class FakeUsersCollection:
    def __init__(self, docs):
        self.docs = docs

    async def find_one(self, query):
        if "_id" in query:
            doc = self.docs.get(str(query["_id"]))
            return None if doc is None else dict(doc)
        if "username" in query:
            for doc in self.docs.values():
                if doc["username"] == query["username"]:
                    return dict(doc)
            return None
        return None

    async def update_one(self, query, update):
        doc = self.docs[str(query["_id"])]
        doc.update(update.get("$set", {}))


def _current_user():
    return CurrentUser(
        _id=ObjectId("507f1f77bcf86cd799439011"),
        username="hugogarsan",
        email="hugo@example.com",
        password="hashed",
        is_active=True,
        full_name="Hugo Garcia",
        bio="Bio inicial",
    )


def test_update_users_me_updates_profile_fields():
    docs = {
        "507f1f77bcf86cd799439011": {
            "_id": ObjectId("507f1f77bcf86cd799439011"),
            "username": "hugogarsan",
            "email": "hugo@example.com",
            "password": "hashed",
            "is_active": True,
            "full_name": "Hugo Garcia",
            "bio": "Bio inicial",
        }
    }
    fake_collection = FakeUsersCollection(docs)
    original_collection = users_routes.users_collection
    users_routes.users_collection = fake_collection

    try:
        response = asyncio.run(
            users_routes.update_users_me(
                UserUpdate(username="hugogarsan2", full_name=" Hugo Garcia de los Santos ", bio="  Nueva bio  "),
                _current_user(),
            )
        )
    finally:
        users_routes.users_collection = original_collection

    assert response.username == "hugogarsan2"
    assert response.full_name == "Hugo Garcia de los Santos"
    assert response.bio == "Nueva bio"


def test_update_users_me_rejects_duplicate_username():
    docs = {
        "507f1f77bcf86cd799439011": {
            "_id": ObjectId("507f1f77bcf86cd799439011"),
            "username": "hugogarsan",
            "email": "hugo@example.com",
            "password": "hashed",
            "is_active": True,
            "full_name": "Hugo Garcia",
            "bio": None,
        },
        "507f1f77bcf86cd799439012": {
            "_id": ObjectId("507f1f77bcf86cd799439012"),
            "username": "otro_usuario",
            "email": "otro@example.com",
            "password": "hashed",
            "is_active": True,
            "full_name": "Otro",
            "bio": None,
        },
    }
    fake_collection = FakeUsersCollection(docs)
    original_collection = users_routes.users_collection
    users_routes.users_collection = fake_collection

    try:
        try:
            asyncio.run(users_routes.update_users_me(UserUpdate(username="otro_usuario"), _current_user()))
            raised = None
        except HTTPException as exc:
            raised = exc
    finally:
        users_routes.users_collection = original_collection

    assert raised is not None
    assert raised.status_code == 400
    assert raised.detail == "Username already registered"
