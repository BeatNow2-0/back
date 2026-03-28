from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import OAuth2PasswordRequestForm

from config.db import users_collection
from config.security import create_access_token, create_refresh_token, verify_password
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from model.user_shemas import LoginResponse

router = APIRouter()


@router.post("/token", response_model=LoginResponse, tags=["auth"])
async def token_login(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    await enforce_rate_limit(request, f"login:{form_data.username}", settings.login_rate_limit)
    user_dict = await users_collection.find_one({"username": form_data.username})
    if not user_dict or not verify_password(form_data.password, user_dict.get("password", "")):
        raise HTTPException(status_code=400, detail="Incorrect username or password")

    access_token = create_access_token(user_dict["username"])
    refresh_token, _ = await create_refresh_token(user_dict["username"])
    return LoginResponse(access_token=access_token, refresh_token=refresh_token)
