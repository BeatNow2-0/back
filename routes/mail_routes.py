from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from config.db import mail_code_collection, password_reset_collection, users_collection
from config.mail import send_email
from config.security import (
    ALGORITHM,
    CONFIRMATION_CODE_EXPIRE_MINUTES,
    PASSWORD_RESET_EXPIRE_MINUTES,
    SECRET_KEY,
    generate_numeric_code,
    get_current_user_without_confirmation,
    get_user_by_email,
    get_user_id,
    hash_password,
)
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from model.shemas import MailCode
from model.user_shemas import ConfirmationRequest, CurrentUser, PasswordResetConfirm, PasswordResetRequest

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
    await mail_code_collection.delete_many({"user_id": user_id})
    await mail_code_collection.insert_one(mail_code.model_dump())
    return confirmation_code


@router.post("/send-confirmation", status_code=status.HTTP_204_NO_CONTENT)
async def send_confirmation(request: Request, user: CurrentUser = Depends(get_current_user_without_confirmation)):
    await enforce_rate_limit(request, f"confirm:{user.username}", settings.confirmation_rate_limit)
    if user.is_active:
        raise HTTPException(status_code=400, detail="User already confirmed")
    confirmation_code = await create_and_save_confirmation_code(user)
    subject = "Confirmación de Registro"
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
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def verify_confirmation_code(user: CurrentUser, provided_code: str) -> bool:
    user_id = await get_user_id(user.username)
    stored_code = await mail_code_collection.find_one({"user_id": user_id})
    if not stored_code:
        raise HTTPException(status_code=404, detail="No code found for this user")
    if stored_code["expires_at"] < _utcnow():
        await mail_code_collection.delete_many({"user_id": user_id})
        raise HTTPException(status_code=400, detail="Confirmation code expired")
    return bcrypt.checkpw(provided_code.encode("utf-8"), stored_code["code"].encode("utf-8"))


@router.post("/confirmation", status_code=status.HTTP_204_NO_CONTENT)
async def confirmation(payload: ConfirmationRequest, user: CurrentUser = Depends(get_current_user_without_confirmation)):
    confirmation = await verify_confirmation_code(user, payload.code)
    if not confirmation:
        raise HTTPException(status_code=400, detail="Invalid code")
    user_id = await get_user_id(user.username)
    await mail_code_collection.delete_many({"user_id": user_id})
    result = await users_collection.update_one({"_id": ObjectId(user_id)}, {"$set": {"is_active": True}})
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to activate user")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/send-password-reset", status_code=status.HTTP_204_NO_CONTENT)
async def send_password_reset(request: Request, payload: PasswordResetRequest):
    await enforce_rate_limit(request, f"reset:{payload.email}", settings.reset_rate_limit)
    user = await get_user_by_email(payload.email)
    if not user:
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    now = _utcnow()
    token_payload = {
        "sub": user.username,
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


@router.post("/password-change", status_code=status.HTTP_204_NO_CONTENT)
async def password_change(payload: PasswordResetConfirm):
    try:
        decoded_payload = jwt.decode(payload.token, SECRET_KEY, algorithms=[ALGORITHM], issuer=settings.app_name)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from exc

    if decoded_payload.get("type") != "password_reset":
        raise HTTPException(status_code=401, detail="Invalid token type")

    token_hash = hashlib.sha256(payload.token.encode("utf-8")).hexdigest()
    reset_doc = await password_reset_collection.find_one({"token_hash": token_hash, "used": False})
    if not reset_doc:
        raise HTTPException(status_code=401, detail="Reset token not found or already used")

    await users_collection.update_one(
        {"username": decoded_payload["sub"]},
        {"$set": {"password": hash_password(payload.new_password)}},
    )
    await password_reset_collection.update_one({"_id": reset_doc["_id"]}, {"$set": {"used": True, "used_at": _utcnow()}})
    return Response(status_code=status.HTTP_204_NO_CONTENT)
