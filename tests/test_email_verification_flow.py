from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import re
from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi.testclient import TestClient

from config import security
from core.rate_limit import _BUCKETS
from main import app
from routes import mail_routes, users_routes


class FakeUsersCollection:
    def __init__(self):
        self.documents: dict[ObjectId, dict] = {}

    def _copy(self, document):
        return deepcopy(document) if document else None

    def _matches(self, document, query):
        for key, value in query.items():
            if key == "$or":
                if not any(self._matches(document, candidate) for candidate in value):
                    return False
                continue
            current = document.get(key)
            if isinstance(value, dict):
                if "$exists" in value and (key in document) != value["$exists"]:
                    return False
                if "$lte" in value and (current is None or current > value["$lte"]):
                    return False
                if "$gt" in value and (current is None or current <= value["$gt"]):
                    return False
                if "$regex" in value:
                    flags = re.IGNORECASE if "i" in value.get("$options", "") else 0
                    if current is None or not re.match(value["$regex"], current, flags):
                        return False
                continue
            if current != value:
                return False
        return True

    async def find_one(self, query, projection=None):
        for document in self.documents.values():
            if self._matches(document, query):
                return self._copy(document)
        return None

    async def insert_one(self, document):
        inserted_id = ObjectId()
        self.documents[inserted_id] = {**deepcopy(document), "_id": inserted_id}
        return SimpleNamespace(inserted_id=inserted_id)

    async def update_one(self, query, update):
        for object_id, document in self.documents.items():
            if self._matches(document, query):
                for key, value in update.get("$set", {}).items():
                    document[key] = value
                for key in update.get("$unset", {}):
                    document.pop(key, None)
                self.documents[object_id] = document
                return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def find_one_and_update(self, query, update, return_document=None):
        for object_id, document in self.documents.items():
            if self._matches(document, query):
                for key, value in update.get("$set", {}).items():
                    document[key] = value
                self.documents[object_id] = document
                return self._copy(document)
        return None


class FakeMailCodeCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}

    def _copy(self, document):
        return deepcopy(document) if document else None

    async def replace_one(self, query, document, upsert=False):
        user_id = query["user_id"]
        previous_id = self.documents.get(user_id, {}).get("_id", ObjectId())
        self.documents[user_id] = {**deepcopy(document), "_id": previous_id}
        return SimpleNamespace(matched_count=1, upserted_id=None)

    async def find_one(self, query, projection=None):
        document = self.documents.get(query.get("user_id"))
        if not document:
            return None
        expires_filter = query.get("expires_at", {})
        if "$gt" in expires_filter and document["expires_at"] <= expires_filter["$gt"]:
            return None
        return self._copy(document)

    async def find_one_and_update(self, query, update, return_document=None):
        for user_id, document in self.documents.items():
            if document.get("_id") == query.get("_id"):
                if "$inc" in update:
                    for key, value in update["$inc"].items():
                        document[key] = document.get(key, 0) + value
                if "$set" in update:
                    document.update(update["$set"])
                self.documents[user_id] = document
                return self._copy(document)
        return None

    async def find_one_and_delete(self, query):
        for user_id, document in list(self.documents.items()):
            if document.get("_id") == query.get("_id"):
                return self.documents.pop(user_id)
        return None

    async def delete_one(self, query):
        for user_id, document in list(self.documents.items()):
            if document.get("_id") == query.get("_id"):
                self.documents.pop(user_id)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


class FakePasswordResetCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}

    async def delete_many(self, query):
        user_id = query.get("user_id")
        deleted = 0
        for token_hash, document in list(self.documents.items()):
            if document.get("user_id") == user_id:
                self.documents.pop(token_hash)
                deleted += 1
        return SimpleNamespace(deleted_count=deleted)

    async def insert_one(self, document):
        self.documents[document["token_hash"]] = deepcopy(document)
        return SimpleNamespace(inserted_id=ObjectId())

    async def find_one_and_update(self, query, update, return_document=None):
        document = self.documents.get(query.get("token_hash"))
        if not document:
            return None
        if document.get("user_id") != query.get("user_id") or document.get("used") != query.get("used"):
            return None
        expires_filter = query.get("expires_at", {})
        if "$gt" in expires_filter and document["expires_at"] <= expires_filter["$gt"]:
            return None
        document.update(update.get("$set", {}))
        return deepcopy(document)


class FakeRefreshTokensCollection:
    def __init__(self):
        self.documents: dict[str, dict] = {}

    async def insert_one(self, document):
        self.documents[document["jti"]] = deepcopy(document)
        return SimpleNamespace(inserted_id=ObjectId())

    async def update_one(self, query, update):
        document = self.documents.get(query.get("jti"))
        if document:
            document.update(update.get("$set", {}))
            return SimpleNamespace(matched_count=1)
        return SimpleNamespace(matched_count=0)

    async def update_many(self, query, update):
        for document in self.documents.values():
            if not document.get("revoked"):
                document.update(update.get("$set", {}))
        return SimpleNamespace(modified_count=len(self.documents))

    async def find_one_and_update(self, query, update, return_document=None):
        document = self.documents.get(query.get("jti"))
        if not document or document.get("revoked") or document.get("expires_at") <= datetime.now(timezone.utc):
            return None
        previous = deepcopy(document)
        document.update(update.get("$set", {}))
        return previous


class CapturedMail(list):
    def __init__(self):
        super().__init__()
        self.reset_tokens: list[str] = []


@pytest.fixture()
def verification_client(monkeypatch):
    _BUCKETS.clear()
    users = FakeUsersCollection()
    mail_codes = FakeMailCodeCollection()
    password_resets = FakePasswordResetCollection()
    refresh_tokens = FakeRefreshTokensCollection()
    sent_codes = CapturedMail()
    generated_codes = iter(
        [
            "111111",
            "222222",
            "333333",
            "444444",
            "555555",
            "666666",
            "777777",
            "888888",
            "999999",
        ]
    )

    monkeypatch.setattr(users_routes, "users_collection", users)
    monkeypatch.setattr(mail_routes, "users_collection", users)
    monkeypatch.setattr(security, "users_collection", users)
    monkeypatch.setattr(mail_routes, "mail_code_collection", mail_codes)
    monkeypatch.setattr(mail_routes, "password_reset_collection", password_resets)
    monkeypatch.setattr(security, "refresh_tokens_collection", refresh_tokens)
    monkeypatch.setattr(users_routes, "create_user_directories", lambda _user_id: None)
    monkeypatch.setattr(mail_routes, "generate_numeric_code", lambda: next(generated_codes))

    async def capture_email(_to, _subject, html):
        for code in ["111111", "222222", "333333", "444444", "555555", "666666", "777777", "888888", "999999"]:
            if code in html:
                sent_codes.append(code)
                break
        if "token=" in html:
            sent_codes.reset_tokens.append(html.split("token=", 1)[1].split("'", 1)[0])

    monkeypatch.setattr(mail_routes, "send_email", capture_email)
    monkeypatch.setattr(users_routes, "send_confirmation_email_to_user", mail_routes.send_confirmation_email_to_user)
    monkeypatch.setattr(security.settings, "confirmation_max_attempts", 3)
    monkeypatch.setattr(mail_routes.settings, "confirmation_max_attempts", 3)
    monkeypatch.setattr(security.settings, "confirmation_resend_cooldown_seconds", 60)
    monkeypatch.setattr(mail_routes.settings, "confirmation_resend_cooldown_seconds", 60)

    return TestClient(app), users, mail_codes, refresh_tokens, sent_codes


def register(client, username="new_user", email="new@example.com", password="correct-password"):
    return client.post(
        "/v1/api/users/register",
        json={"username": username, "email": email, "password": password},
    )


def login(client, username="new_user", password="correct-password"):
    return client.post(
        "/v1/api/users/login",
        data={"username": username, "password": password},
        headers={"content-type": "application/x-www-form-urlencoded"},
    )


def test_register_returns_verification_token_inactive_user_and_code(verification_client):
    client, users, mail_codes, _refresh_tokens, sent_codes = verification_client

    response = register(client)

    assert response.status_code == 201
    body = response.json()
    assert body["message"] == "Verification required"
    assert body["verification_required"] is True
    assert body["verification_token"]
    assert body["expires_in"] == 600
    assert "access_token" not in body
    user = next(iter(users.documents.values()))
    assert user["is_active"] is False
    assert mail_codes.documents[str(user["_id"])]["attempts"] == 0
    assert sent_codes == ["111111"]


def test_verification_token_cannot_use_users_me_and_access_token_cannot_confirm(verification_client):
    client, _users, _mail_codes, _refresh_tokens, _sent_codes = verification_client
    verification_token = register(client).json()["verification_token"]

    users_me = client.get("/v1/api/users/users/me", headers={"Authorization": f"Bearer {verification_token}"})
    assert users_me.status_code == 401

    access_token = security.create_access_token("507f1f77bcf86cd799439011")
    confirmation = client.post(
        "/v1/api/mail/confirmation",
        json={"code": "111111"},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert confirmation.status_code == 401


def test_valid_confirmation_activates_user_and_consumes_code(verification_client):
    client, users, mail_codes, _refresh_tokens, _sent_codes = verification_client
    verification_token = register(client).json()["verification_token"]

    response = client.post(
        "/v1/api/mail/confirmation",
        json={"code": "111111"},
        headers={"Authorization": f"Bearer {verification_token}"},
    )

    assert response.status_code == 204
    user = next(iter(users.documents.values()))
    assert user["is_active"] is True
    assert str(user["_id"]) not in mail_codes.documents

    reused = client.post(
        "/v1/api/mail/confirmation",
        json={"code": "111111"},
        headers={"Authorization": f"Bearer {verification_token}"},
    )
    assert reused.status_code == 400


def test_invalid_expired_and_max_attempt_codes_fail(verification_client):
    client, users, mail_codes, _refresh_tokens, _sent_codes = verification_client
    token = register(client, username="wrong_user", email="wrong@example.com").json()["verification_token"]

    wrong = client.post("/v1/api/mail/confirmation", json={"code": "000000"}, headers={"Authorization": f"Bearer {token}"})
    assert wrong.status_code == 400

    user = next(iter(users.documents.values()))
    mail_codes.documents[str(user["_id"])]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    expired = client.post("/v1/api/mail/confirmation", json={"code": "111111"}, headers={"Authorization": f"Bearer {token}"})
    assert expired.status_code == 400

    token = register(client, username="attempt_user", email="attempt@example.com").json()["verification_token"]
    attempt_user = [document for document in users.documents.values() if document["username"] == "attempt_user"][0]
    for _ in range(3):
        client.post("/v1/api/mail/confirmation", json={"code": "000000"}, headers={"Authorization": f"Bearer {token}"})
    assert str(attempt_user["_id"]) not in mail_codes.documents


def test_inactive_login_returns_verification_state_and_wrong_password_does_not(verification_client):
    client, _users, _mail_codes, _refresh_tokens, _sent_codes = verification_client
    register(client)

    inactive = login(client, username="NEW_USER")
    assert inactive.status_code == 403
    body = inactive.json()
    assert body["detail"] == "Account verification required"
    assert body["code"] == "ACCOUNT_NOT_VERIFIED"
    assert body["verification_required"] is True
    assert body["verification_token"]
    assert body["expires_in"] == 600

    wrong_password = login(client, password="wrong-password")
    assert wrong_password.status_code == 401
    assert "verification_token" not in wrong_password.json()
    assert wrong_password.json()["detail"] == "Incorrect username or password"


def test_resend_rate_limit_and_new_code_invalidates_old_code(verification_client):
    client, users, _mail_codes, _refresh_tokens, sent_codes = verification_client
    token = register(client).json()["verification_token"]
    user = next(iter(users.documents.values()))
    users.documents[user["_id"]]["confirmation_last_sent_at"] = datetime.now(timezone.utc) - timedelta(seconds=61)

    resent = client.post("/v1/api/mail/send-confirmation", headers={"Authorization": f"Bearer {token}"})
    assert resent.status_code == 200
    assert resent.json() == {"message": "Confirmation code sent", "retry_after": 60}
    assert sent_codes[-1] == "222222"

    old_code = client.post("/v1/api/mail/confirmation", json={"code": "111111"}, headers={"Authorization": f"Bearer {token}"})
    assert old_code.status_code == 400

    limited = client.post("/v1/api/mail/send-confirmation", headers={"Authorization": f"Bearer {token}"})
    assert limited.status_code == 429
    assert limited.json()["detail"] == "Please wait before requesting another code"
    assert 1 <= limited.json()["retry_after"] <= 60

    users.documents[user["_id"]]["confirmation_last_sent_at"] = datetime.now(timezone.utc) - timedelta(seconds=61)
    resent_again = client.post("/v1/api/mail/send-confirmation", headers={"Authorization": f"Bearer {token}"})
    assert resent_again.status_code == 200
    assert sent_codes[-1] == "333333"


def test_active_user_cannot_start_confirmation_and_active_login_refresh_logout_work(verification_client):
    client, users, _mail_codes, refresh_tokens, _sent_codes = verification_client
    token = register(client).json()["verification_token"]
    confirmation = client.post("/v1/api/mail/confirmation", json={"code": "111111"}, headers={"Authorization": f"Bearer {token}"})
    assert confirmation.status_code == 204

    resend = client.post("/v1/api/mail/send-confirmation", headers={"Authorization": f"Bearer {token}"})
    assert resend.status_code == 400

    active_login = login(client, username="NEW_USER")
    assert active_login.status_code == 200
    tokens = active_login.json()
    assert tokens["access_token"]
    assert tokens["refresh_token"]

    refresh = client.post("/v1/api/users/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refresh.status_code == 200
    refreshed = refresh.json()
    assert refreshed["access_token"]
    assert refreshed["refresh_token"] != tokens["refresh_token"]

    logout = client.post("/v1/api/users/logout", json={"refresh_token": refreshed["refresh_token"]})
    assert logout.status_code == 204
    assert any(document.get("revoked") for document in refresh_tokens.documents.values())
    assert next(iter(users.documents.values()))["is_active"] is True


def test_password_reset_aliases_change_password_once(verification_client):
    client, users, _mail_codes, _refresh_tokens, sent_codes = verification_client
    verification_token = register(client, username="reset_user", email="Reset@Example.com").json()["verification_token"]
    confirmation = client.post(
        "/v1/api/mail/confirmation",
        json={"code": "111111"},
        headers={"Authorization": f"Bearer {verification_token}"},
    )
    assert confirmation.status_code == 204
    user = next(iter(users.documents.values()))
    assert user["email"] == "reset@example.com"

    reset_request = client.post("/v1/api/mail/forgot-password", json={"email": " RESET@example.com "})
    assert reset_request.status_code == 204
    assert sent_codes.reset_tokens

    old_login = login(client, username="reset_user")
    assert old_login.status_code == 200

    token = sent_codes.reset_tokens[-1]
    reset = client.post(
        "/v1/api/mail/reset-password",
        json={"token": token, "newPassword": "new-correct-password"},
    )
    assert reset.status_code == 204

    reused = client.post(
        "/v1/api/mail/password-change",
        json={"token": token, "password": "another-password"},
    )
    assert reused.status_code == 401

    stale_login = login(client, username="reset_user")
    assert stale_login.status_code == 401
    fresh_login = login(client, username="reset_user", password="new-correct-password")
    assert fresh_login.status_code == 200


def test_openapi_exposes_verification_contract(verification_client):
    client, _users, _mail_codes, _refresh_tokens, _sent_codes = verification_client
    schema = client.get("/openapi.json").json()

    register_schema = schema["paths"]["/v1/api/users/register"]["post"]["responses"]["201"]
    inactive_schema = schema["paths"]["/v1/api/users/login"]["post"]["responses"]["403"]
    resend_schema = schema["paths"]["/v1/api/mail/send-confirmation"]["post"]["responses"]["200"]
    rate_limit_schema = schema["paths"]["/v1/api/mail/send-confirmation"]["post"]["responses"]["429"]
    assert "VerificationRequiredResponse" in str(register_schema)
    assert "InactiveLoginResponse" in str(inactive_schema)
    assert "ConfirmationSentResponse" in str(resend_schema)
    assert "RetryAfterErrorResponse" in str(rate_limit_schema)
