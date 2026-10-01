from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from uuid import uuid4

import jwt
from bson import ObjectId
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt import ExpiredSignatureError, PyJWTError
from passlib.context import CryptContext

from config.db import post_collection, refresh_tokens_collection, users_collection
from config.settings import settings
from core.mongo import parse_object_id
from model.user_shemas import CurrentUser
from pymongo import ReturnDocument

logger = logging.getLogger(__name__)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/v1/api/users/login")
pwd_context = CryptContext(schemes=["bcrypt_sha256", "bcrypt"], deprecated=["bcrypt"])

SECRET_KEY = settings.secret_key
ALGORITHM = settings.algorithm
ACCESS_TOKEN_EXPIRE_MINUTES = settings.access_token_expire_minutes
REFRESH_TOKEN_EXPIRE_MINUTES = settings.refresh_token_expire_minutes
PASSWORD_RESET_EXPIRE_MINUTES = settings.password_reset_expire_minutes
CONFIRMATION_CODE_EXPIRE_MINUTES = settings.confirmation_code_expire_minutes


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    return pwd_context.verify(plain_password, hashed_password)


async def authenticate_user(username: str, password: str) -> CurrentUser:
    user_dict = await users_collection.find_one({"username": username})
    if not user_dict or not verify_password(password, user_dict.get("password", "")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = CurrentUser(**user_dict)
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user


async def issue_token_pair(user: CurrentUser) -> tuple[str, str]:
    subject = str(user.id)
    access_token = create_access_token(subject)
    refresh_token, _ = await create_refresh_token(subject, username=user.username)
    return access_token, refresh_token


async def get_user(username: str) -> Optional[CurrentUser]:
    user = await users_collection.find_one({"username": username})
    if user:
        return CurrentUser(**user)
    return None


async def get_user_by_email(email: str) -> Optional[CurrentUser]:
    user = await users_collection.find_one({"email": email})
    if user:
        return CurrentUser(**user)
    return None


async def get_user_by_subject(subject: str) -> Optional[CurrentUser]:
    if ObjectId.is_valid(subject):
        user = await users_collection.find_one({"_id": ObjectId(subject)})
        if user:
            return CurrentUser(**user)
    return await get_user(subject)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(subject: str) -> str:
    now = _utcnow()
    payload = {
        "sub": subject,
        "type": "access",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)).timestamp()),
        "iss": settings.app_name,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def create_refresh_token(subject: str, username: str | None = None) -> tuple[str, str]:
    now = _utcnow()
    jti = str(uuid4())
    payload = {
        "sub": subject,
        "jti": jti,
        "type": "refresh",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES)).timestamp()),
        "iss": settings.app_name,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)
    await refresh_tokens_collection.insert_one(
        {
            "jti": jti,
            "user_id": subject if ObjectId.is_valid(subject) else None,
            "username": username or (subject if not ObjectId.is_valid(subject) else None),
            "created_at": now,
            "expires_at": now + timedelta(minutes=REFRESH_TOKEN_EXPIRE_MINUTES),
            "revoked": False,
        }
    )
    return token, jti


def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM], issuer=settings.app_name)


async def get_current_user(token: Annotated[str, Depends(oauth2_scheme)]) -> CurrentUser:
    try:
        payload = decode_token(token)
    except ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from exc
    except PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    subject = payload.get("sub")
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")

    user = await get_user_by_subject(subject)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user


async def get_current_user_without_confirmation(token: Annotated[str, Depends(oauth2_scheme)]) -> CurrentUser:
    try:
        payload = decode_token(token)
    except ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token expired") from exc
    except PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token") from exc

    if payload.get("type") != "access":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    subject = payload.get("sub")
    if not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    user = await get_user_by_subject(subject)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def revoke_refresh_token(jti: str) -> None:
    await refresh_tokens_collection.update_one({"jti": jti}, {"$set": {"revoked": True, "revoked_at": _utcnow()}})


async def revoke_refresh_token_value(token: str) -> None:
    try:
        payload = decode_token(token)
    except PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc
    if payload.get("type") != "refresh" or not payload.get("jti"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    await revoke_refresh_token(payload["jti"])


async def revoke_all_refresh_tokens(user_id: str, username: str | None = None) -> None:
    identities: list[dict[str, str]] = [{"user_id": user_id}]
    if username:
        identities.append({"username": username})
    await refresh_tokens_collection.update_many(
        {"$or": identities, "revoked": False},
        {"$set": {"revoked": True, "revoked_at": _utcnow()}},
    )


async def consume_refresh_token(token: str) -> CurrentUser:
    try:
        payload = decode_token(token)
    except ExpiredSignatureError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token expired") from exc
    except PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token") from exc

    if payload.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    jti = payload.get("jti")
    subject = payload.get("sub")
    if not jti or not subject:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token")
    token_doc = await refresh_tokens_collection.find_one_and_update(
        {"jti": jti, "revoked": False, "expires_at": {"$gt": _utcnow()}},
        {"$set": {"revoked": True, "revoked_at": _utcnow(), "replaced": True}},
        return_document=ReturnDocument.BEFORE,
    )
    if not token_doc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Refresh token revoked")

    user = await get_user_by_subject(subject)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive user")
    return user


validate_refresh_token = consume_refresh_token


async def get_user_id(username: str) -> str:
    user = await users_collection.find_one({"username": username}, {"_id": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return str(user["_id"])


async def get_username(user_id: str) -> str:
    user = await users_collection.find_one({"_id": parse_object_id(user_id, field="user identifier")}, {"username": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user["username"]


async def get_post_owner_id(post_id: str) -> str:
    post = await post_collection.find_one({"_id": parse_object_id(post_id, field="post identifier")}, {"user_id": 1})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return str(post["user_id"])


async def check_post_exists(post_id: str, db=None):
    post = await post_collection.find_one({"_id": parse_object_id(post_id, field="post identifier")}, {"_id": 1})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")


def generate_numeric_code(length: int = 6) -> str:
    max_number = 10 ** length
    return str(secrets.randbelow(max_number)).zfill(length)
