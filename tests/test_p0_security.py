import asyncio
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import jwt
import pytest
from bson import ObjectId
from fastapi import HTTPException

from config import security
from config.settings import settings
from core.mongo import parse_object_id
from core.rate_limit import _BUCKETS
from model.user_shemas import RegisterRequest
from routes import mail_routes, users_routes


class DummyRequest:
    headers = {}
    client = SimpleNamespace(host="127.0.0.1")


class FakeRegistrationUsers:
    def __init__(self):
        self.document = None
        self.inserted_id = ObjectId("507f1f77bcf86cd799439011")

    async def find_one(self, query, projection=None):
        if self.document and query.get("_id") == self.inserted_id:
            return dict(self.document)
        return None

    async def insert_one(self, document):
        self.document = {**document, "_id": self.inserted_id}
        return SimpleNamespace(inserted_id=self.inserted_id)


class ExistingRegistrationUser:
    def __init__(self, field):
        self.field = field

    async def find_one(self, query, projection=None):
        return {"_id": ObjectId()} if self.field in query else None


def test_register_ignores_client_is_active(monkeypatch):
    collection = FakeRegistrationUsers()
    monkeypatch.setattr(users_routes, "users_collection", collection)
    monkeypatch.setattr(users_routes, "create_user_directories", lambda _user_id: None)

    async def no_email(_user):
        return None

    monkeypatch.setattr(users_routes, "send_confirmation_email_to_user", no_email)
    request = RegisterRequest.model_validate(
        {
            "username": "beta_user",
            "email": "beta@example.com",
            "password": "correct-password",
            "is_active": True,
        }
    )

    asyncio.run(users_routes.register(request, DummyRequest()))
    assert collection.document["is_active"] is False


@pytest.mark.parametrize(
    ("field", "detail"),
    [("username", "Username already registered"), ("email", "Email already registered")],
)
def test_register_rejects_duplicate_identity(monkeypatch, field, detail):
    monkeypatch.setattr(users_routes, "users_collection", ExistingRegistrationUser(field))
    request = RegisterRequest(
        username="duplicate_user",
        email="duplicate@example.com",
        password="correct-password",
    )
    with pytest.raises(HTTPException) as exc:
        asyncio.run(users_routes.register(request, DummyRequest()))
    assert exc.value.status_code == 400
    assert exc.value.detail == detail


class AuthenticationUsers:
    def __init__(self, document):
        self.document = document

    async def find_one(self, query, projection=None):
        username = query.get("username")
        if isinstance(username, dict) and "$regex" in username:
            flags = re.IGNORECASE if "i" in username.get("$options", "") else 0
            return dict(self.document) if re.match(username["$regex"], self.document["username"], flags) else None
        return dict(self.document) if username == self.document["username"] else None


def test_login_authentication_success_wrong_password_and_inactive(monkeypatch):
    document = {
        "_id": ObjectId("507f1f77bcf86cd799439011"),
        "username": "beta_user",
        "email": "beta@example.com",
        "password": security.hash_password("correct-password"),
        "is_active": True,
    }
    monkeypatch.setattr(security, "users_collection", AuthenticationUsers(document))
    user = asyncio.run(security.authenticate_user("BETA_USER", "correct-password"))
    assert user.username == "beta_user"

    with pytest.raises(HTTPException) as wrong_password:
        asyncio.run(security.authenticate_user("beta_user", "wrong-password"))
    assert wrong_password.value.status_code == 401
    assert wrong_password.value.headers == {"WWW-Authenticate": "Bearer"}

    document["is_active"] = False
    with pytest.raises(HTTPException) as inactive:
        asyncio.run(security.authenticate_user("beta_user", "correct-password"))
    assert inactive.value.status_code == 403


@pytest.mark.parametrize("value", ["", "123", "z" * 24, "../etc/passwd"])
def test_invalid_object_id_returns_422(value):
    with pytest.raises(HTTPException) as exc:
        parse_object_id(value, field="post identifier")
    assert exc.value.status_code == 422


def test_valid_object_id_is_parsed():
    value = "507f1f77bcf86cd799439011"
    assert parse_object_id(value) == ObjectId(value)


class AtomicResetCollection:
    def __init__(self, document):
        self.document = document
        self.lock = asyncio.Lock()

    async def find_one_and_update(self, query, update, return_document=None):
        async with self.lock:
            if self.document["used"] or self.document["expires_at"] <= datetime.now(timezone.utc):
                return None
            self.document.update(update["$set"])
            return dict(self.document)


class ResetUsers:
    def __init__(self, user_id):
        self.user_id = user_id

    async def find_one(self, query, projection=None):
        return {"_id": self.user_id, "username": "beta_user"}

    async def update_one(self, query, update):
        return SimpleNamespace(matched_count=1)


def test_password_reset_token_is_consumed_once(monkeypatch):
    user_id = ObjectId("507f1f77bcf86cd799439011")
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(user_id),
            "type": "password_reset",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iss": settings.app_name,
        },
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    document = {
        "_id": ObjectId(),
        "user_id": str(user_id),
        "token_hash": mail_routes.hashlib.sha256(token.encode()).hexdigest(),
        "expires_at": now + timedelta(minutes=5),
        "used": False,
    }
    monkeypatch.setattr(mail_routes, "password_reset_collection", AtomicResetCollection(document))
    monkeypatch.setattr(mail_routes, "users_collection", ResetUsers(user_id))
    monkeypatch.setattr(mail_routes, "hash_password", lambda value: f"hashed:{value}")

    revoked_users = []

    async def no_revoke(*args):
        revoked_users.append(args)

    monkeypatch.setattr(mail_routes, "revoke_all_refresh_tokens", no_revoke)

    async def attempt():
        try:
            await mail_routes.password_change(
                mail_routes.PasswordResetConfirm(token=token, new_password="updated-password"),
                DummyRequest(),
            )
            return 204
        except HTTPException as exc:
            return exc.status_code

    results = asyncio.run(_gather(attempt, 2))
    assert sorted(results) == [204, 401]
    assert revoked_users == [(str(user_id), "beta_user")]

    _BUCKETS.clear()
    assert asyncio.run(attempt()) == 401

    expired = {**document, "used": False, "expires_at": now - timedelta(seconds=1)}
    monkeypatch.setattr(mail_routes, "password_reset_collection", AtomicResetCollection(expired))
    _BUCKETS.clear()
    assert asyncio.run(attempt()) == 401


async def _gather(factory, count):
    return await asyncio.gather(*(factory() for _ in range(count)))


class AtomicRefreshCollection:
    def __init__(self, document):
        self.document = document
        self.lock = asyncio.Lock()

    async def find_one_and_update(self, query, update, return_document=None):
        async with self.lock:
            if self.document["revoked"]:
                return None
            previous = dict(self.document)
            self.document.update(update["$set"])
            return previous


def test_refresh_token_rotation_is_atomic(monkeypatch):
    user_id = ObjectId("507f1f77bcf86cd799439011")
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(user_id),
            "jti": "one-use-token",
            "type": "refresh",
            "iat": int(now.timestamp()),
            "nbf": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
            "iss": settings.app_name,
        },
        settings.secret_key,
        algorithm=settings.algorithm,
    )
    monkeypatch.setattr(
        security,
        "refresh_tokens_collection",
        AtomicRefreshCollection(
            {"jti": "one-use-token", "user_id": str(user_id), "expires_at": now + timedelta(minutes=5), "revoked": False}
        ),
    )

    async def get_subject(_subject):
        return security.CurrentUser(
            _id=user_id,
            username="beta_user",
            email="beta@example.com",
            password="hashed",
            is_active=True,
        )

    monkeypatch.setattr(security, "get_user_by_subject", get_subject)

    async def attempt():
        try:
            await security.consume_refresh_token(token)
            return 200
        except HTTPException as exc:
            return exc.status_code

    results = asyncio.run(_gather(attempt, 2))
    assert sorted(results) == [200, 401]
