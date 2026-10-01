from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm

from config.security import (
    authenticate_user_credentials,
    create_email_verification_token,
    get_email_verification_expires_in,
    issue_token_pair,
    normalize_login_username,
)
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from model.user_shemas import InactiveLoginResponse, LoginResponse

router = APIRouter()


@router.post(
    "/token",
    response_model=LoginResponse,
    tags=["auth"],
    responses={status.HTTP_403_FORBIDDEN: {"model": InactiveLoginResponse}},
)
async def token_login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    await enforce_rate_limit(request, f"login:{normalize_login_username(form_data.username)}", settings.login_rate_limit)
    user = await authenticate_user_credentials(form_data.username, form_data.password)
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=InactiveLoginResponse(
                verification_token=create_email_verification_token(str(user.id)),
                expires_in=get_email_verification_expires_in(),
            ).model_dump(),
        )
    access_token, refresh_token = await issue_token_pair(user)
    return LoginResponse(access_token=access_token, refresh_token=refresh_token)
