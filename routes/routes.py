from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm

from config.security import authenticate_user, issue_token_pair
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from model.user_shemas import LoginResponse

router = APIRouter()


@router.post("/token", response_model=LoginResponse, tags=["auth"])
async def token_login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    await enforce_rate_limit(request, f"login:{form_data.username}", settings.login_rate_limit)
    user = await authenticate_user(form_data.username, form_data.password)
    access_token, refresh_token = await issue_token_pair(user)
    return LoginResponse(access_token=access_token, refresh_token=refresh_token)
