from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pymongo import ReturnDocument

from config.db import mail_code_collection, password_reset_collection, users_collection
from config.mail import send_email
from config.security import (
    ALGORITHM,
    CONFIRMATION_CODE_EXPIRE_MINUTES,
    PASSWORD_RESET_EXPIRE_MINUTES,
    SECRET_KEY,
    generate_numeric_code,
    get_current_user_for_email_verification,
    get_user_by_email,
    get_user_id,
    hash_password,
    revoke_all_refresh_tokens,
)
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from core.mongo import parse_object_id
from model.shemas import MailCode
from model.user_shemas import (
    ConfirmationRequest,
    ConfirmationSentResponse,
    CurrentUser,
    PasswordResetConfirm,
    PasswordResetRequest,
    RetryAfterErrorResponse,
)

router = APIRouter()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def create_and_save_confirmation_code(user: CurrentUser) -> str:
    confirmation_code = generate_numeric_code()
    code_hash = bcrypt.hashpw(confirmation_code.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user_id = await get_user_id(user.username)
    mail_code = MailCode(
        user_id=user_id,
        code=code_hash,
        expires_at=_utcnow() + timedelta(minutes=CONFIRMATION_CODE_EXPIRE_MINUTES),
    )
    code_document = mail_code.model_dump()
    code_document["attempts"] = 0
    await mail_code_collection.replace_one({"user_id": user_id}, code_document, upsert=True)
    return confirmation_code


async def send_confirmation_email_to_user(user: CurrentUser, *, update_last_sent: bool = True) -> None:
    confirmation_code = await create_and_save_confirmation_code(user)
    subject = "Confirmacion de Registro"
    html_content = f"""
    <html><body>
        <h1>Verify your email address</h1>
        <p>Hello {user.username},</p>
        <p>Your verification code is:</p>
        <h2>{confirmation_code}</h2>
        <p>This code expires in {CONFIRMATION_CODE_EXPIRE_MINUTES} minutes.</p>
    </body></html>
    """
    await send_email(user.email, subject, html_content)
    if update_last_sent:
        await users_collection.update_one(
            {"_id": parse_object_id(str(user.id), field="user identifier")},
            {"$set": {"confirmation_last_sent_at": _utcnow()}},
        )


def _seconds_until_resend(last_sent_at: datetime) -> int:
    if last_sent_at.tzinfo is None:
        last_sent_at = last_sent_at.replace(tzinfo=timezone.utc)
    elapsed = (_utcnow() - last_sent_at).total_seconds()
    return max(0, int(settings.confirmation_resend_cooldown_seconds - elapsed + 0.999))


async def reserve_confirmation_resend(user: CurrentUser) -> int:
    user_id = parse_object_id(str(user.id), field="user identifier")
    now = _utcnow()
    cutoff = now - timedelta(seconds=settings.confirmation_resend_cooldown_seconds)
    updated = await users_collection.find_one_and_update(
        {
            "_id": user_id,
            "is_active": False,
            "$or": [
                {"confirmation_last_sent_at": {"$exists": False}},
                {"confirmation_last_sent_at": {"$lte": cutoff}},
            ],
        },
        {"$set": {"confirmation_last_sent_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if updated:
        return settings.confirmation_resend_cooldown_seconds

    current = await users_collection.find_one({"_id": user_id}, {"is_active": 1, "confirmation_last_sent_at": 1})
    if not current:
        raise HTTPException(status_code=401, detail="Invalid verification token subject")
    if current.get("is_active"):
        raise HTTPException(status_code=400, detail="User already confirmed")
    last_sent_at = current.get("confirmation_last_sent_at")
    retry_after = _seconds_until_resend(last_sent_at) if isinstance(last_sent_at, datetime) else settings.confirmation_resend_cooldown_seconds
    raise HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "detail": "Please wait before requesting another code",
            "retry_after": retry_after,
        },
    )


@router.post(
    "/send-confirmation",
    response_model=ConfirmationSentResponse,
    responses={status.HTTP_429_TOO_MANY_REQUESTS: {"model": RetryAfterErrorResponse}},
)
async def send_confirmation(request: Request, user: CurrentUser = Depends(get_current_user_for_email_verification)):
    await enforce_rate_limit(request, f"confirm:{user.username}", settings.confirmation_rate_limit)
    if user.is_active:
        raise HTTPException(status_code=400, detail="User already confirmed")
    retry_after = await reserve_confirmation_resend(user)
    await send_confirmation_email_to_user(user, update_last_sent=False)
    return ConfirmationSentResponse(retry_after=retry_after)


async def verify_confirmation_code(user: CurrentUser, provided_code: str) -> dict | None:
    user_id = await get_user_id(user.username)
    stored_code = await mail_code_collection.find_one({"user_id": user_id, "expires_at": {"$gt": _utcnow()}})
    if not stored_code:
        return None
    if not bcrypt.checkpw(provided_code.encode("utf-8"), stored_code["code"].encode("utf-8")):
        updated = await mail_code_collection.find_one_and_update(
            {"_id": stored_code["_id"]},
            {"$inc": {"attempts": 1}},
            return_document=ReturnDocument.AFTER,
        )
        if updated and updated.get("attempts", 0) >= settings.confirmation_max_attempts:
            await mail_code_collection.delete_one({"_id": stored_code["_id"]})
        return None
    return stored_code


@router.post("/confirmation", status_code=status.HTTP_204_NO_CONTENT)
async def confirmation(
    payload: ConfirmationRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user_for_email_verification),
):
    await enforce_rate_limit(request, f"confirm-code:{user.username}", settings.confirmation_rate_limit)
    if user.is_active:
        raise HTTPException(status_code=400, detail="User already confirmed")
    confirmation_code = await verify_confirmation_code(user, payload.code)
    if not confirmation_code:
        raise HTTPException(status_code=400, detail="Invalid code")
    user_id = await get_user_id(user.username)
    consumed = await mail_code_collection.find_one_and_delete({"_id": confirmation_code["_id"]})
    if not consumed:
        raise HTTPException(status_code=400, detail="Invalid or already used code")
    result = await users_collection.update_one(
        {"_id": parse_object_id(user_id, field="user identifier")},
        {"$set": {"is_active": True}, "$unset": {"confirmation_last_sent_at": ""}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="User not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _send_password_reset(request: Request, payload: PasswordResetRequest) -> Response:
    await enforce_rate_limit(request, f"reset:{payload.email}", settings.reset_rate_limit)
    user = await get_user_by_email(payload.email)
    if not user:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    now = _utcnow()
    token_payload = {
        "sub": str(user.id),
        "type": "password_reset",
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES)).timestamp()),
        "iss": settings.app_name,
    }
    token = jwt.encode(token_payload, SECRET_KEY, algorithm=ALGORITHM)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    await password_reset_collection.delete_many({"user_id": await get_user_id(user.username)})
    await password_reset_collection.insert_one(
        {
            "user_id": await get_user_id(user.username),
            "token_hash": token_hash,
            "created_at": now,
            "expires_at": now + timedelta(minutes=PASSWORD_RESET_EXPIRE_MINUTES),
            "used": False,
        }
    )
    reset_link = f"{settings.public_base_url.rstrip('/')}/reset-password?token={token}"
    html_content = f"<html><body><p>Hello {user.username},</p><p>Reset your password:</p><a href='{reset_link}'>{reset_link}</a><p>This link expires in {PASSWORD_RESET_EXPIRE_MINUTES} minutes.</p></body></html>"
    await send_email(user.email, "Password Reset", html_content)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/send-password-reset", status_code=status.HTTP_204_NO_CONTENT)
@router.post("/forgot-password", status_code=status.HTTP_204_NO_CONTENT)
async def send_password_reset(request: Request, payload: PasswordResetRequest):
    return await _send_password_reset(request, payload)


async def _password_change(payload: PasswordResetConfirm, request: Request) -> Response:
    await enforce_rate_limit(request, "password-change", settings.reset_rate_limit)
    try:
        decoded_payload = jwt.decode(payload.token, SECRET_KEY, algorithms=[ALGORITHM], issuer=settings.app_name)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    if decoded_payload.get("type") != "password_reset":
        raise HTTPException(status_code=401, detail="Invalid token type")

    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    subject = decoded_payload.get("sub")
    if not subject:
        raise HTTPException(status_code=401, detail="Invalid token payload")
    if ObjectId.is_valid(subject):
        user_object_id = ObjectId(subject)
        user = await users_collection.find_one({"_id": user_object_id}, {"username": 1})
    else:
        user = await users_collection.find_one({"username": subject}, {"username": 1})
        user_object_id = user["_id"] if user else None
    if not user or user_object_id is None:
        raise HTTPException(status_code=401, detail="Invalid reset token subject")
    user_id = str(user_object_id)
    now = _utcnow()
    reset_doc = await password_reset_collection.find_one_and_update(
        {
            "token_hash": token_hash,
            "user_id": user_id,
            "used": False,
            "expires_at": {"$gt": now},
        },
        {"$set": {"used": True, "used_at": now}},
        return_document=ReturnDocument.AFTER,
    )
    if not reset_doc:
        raise HTTPException(status_code=401, detail="Reset token not found or already used")

    result = await users_collection.update_one(
        {"_id": user_object_id},
        {"$set": {"password": hash_password(payload.new_password)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=401, detail="Invalid reset token subject")
    await revoke_all_refresh_tokens(user_id, user.get("username"))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/password-change", status_code=status.HTTP_204_NO_CONTENT)
@router.post("/reset-password", status_code=status.HTTP_204_NO_CONTENT)
async def password_change(payload: PasswordResetConfirm, request: Request):
    return await _password_change(payload, request)
