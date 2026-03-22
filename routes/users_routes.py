from __future__ import annotations

import logging

import jwt
from typing import Annotated, List

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm

from config.db import (
    follows_collection,
    get_database,
    interactions_collection,
    lyrics_collection,
    password_reset_collection,
    post_collection,
    refresh_tokens_collection,
    users_collection,
)
from config.security import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    get_current_user_without_confirmation,
    get_post_owner_id,
    get_user,
    get_user_id,
    revoke_refresh_token,
    validate_refresh_token,
    verify_password,
    hash_password,
)
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from model.lyrics_shemas import LyricsInDB
from model.user_shemas import CurrentUser, LoginResponse, NewUser, RefreshTokenRequest, UserPublic
from services.storage import create_user_directories, delete_user_directories

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def register(user: NewUser):
    if await users_collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="Username already registered")
    if await users_collection.find_one({"email": user.email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    user_dict = user.model_dump()
    user_dict["password"] = hash_password(user.password)
    result = await users_collection.insert_one(user_dict)
    user_id = str(result.inserted_id)
    create_user_directories(user_id)
    created = await users_collection.find_one({"_id": result.inserted_id})
    return UserPublic(**created)


@router.delete("/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(current_user: CurrentUser = Depends(get_current_user)):
    user_id = await get_user_id(current_user.username)
    user_posts = await post_collection.find({"user_id": user_id}, {"_id": 1}).to_list(None)
    post_ids = [post["_id"] for post in user_posts]

    await follows_collection.delete_many({"user_id_following": user_id})
    await follows_collection.delete_many({"user_id_followed": user_id})
    await lyrics_collection.delete_many({"user_id": user_id})
    await interactions_collection.delete_many({"post_id": {"$in": [str(pid) for pid in post_ids]}})
    await interactions_collection.delete_many({"user_id": user_id})
    await post_collection.delete_many({"user_id": user_id})
    await password_reset_collection.delete_many({"user_id": user_id})
    await refresh_tokens_collection.delete_many({"username": current_user.username})
    await users_collection.delete_one({"_id": ObjectId(user_id)})
    delete_user_directories(user_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users/me", response_model=UserPublic)
async def read_users_me(current_user: Annotated[CurrentUser, Depends(get_current_user_without_confirmation)]):
    return UserPublic(**current_user.model_dump(by_alias=True))


@router.post("/login", response_model=LoginResponse)
async def login_for_access_token(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    await enforce_rate_limit(request, f"login:{form_data.username}", settings.login_rate_limit)
    user_dict = await users_collection.find_one({"username": form_data.username})
    if not user_dict or not verify_password(form_data.password, user_dict.get("password", "")):
        raise HTTPException(status_code=400, detail="Incorrect username or password")
    if not user_dict.get("is_active"):
        raise HTTPException(status_code=403, detail="Account not confirmed")

    access_token = create_access_token(user_dict["username"])
    refresh_token, _ = await create_refresh_token(user_dict["username"])
    return LoginResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/refresh", response_model=LoginResponse)
async def refresh_access_token(payload: RefreshTokenRequest):
    user = await validate_refresh_token(payload.refresh_token)
    decoded = jwt.decode(payload.refresh_token, settings.secret_key, algorithms=[settings.algorithm], issuer=settings.app_name)
    await revoke_refresh_token(decoded["jti"])
    access_token = create_access_token(user.username)
    refresh_token, _ = await create_refresh_token(user.username)
    return LoginResponse(access_token=access_token, refresh_token=refresh_token)


@router.get("/saved-posts")
async def get_saved_posts(current_user: CurrentUser = Depends(get_current_user)):
    user_id = await get_user_id(current_user.username)
    saved_posts = await interactions_collection.find({"user_id": user_id, "saved_date": {"$exists": True}}).to_list(None)
    for post in saved_posts:
        post["_id"] = str(post["_id"])
        post["creator_id"] = await get_post_owner_id(post["post_id"])
    return {"saved_posts": saved_posts}


@router.get("/liked-posts")
async def get_liked_posts(current_user: CurrentUser = Depends(get_current_user)):
    user_id = await get_user_id(current_user.username)
    liked_posts = await interactions_collection.find({"user_id": user_id, "like_date": {"$exists": True}}).to_list(None)
    for post in liked_posts:
        post["_id"] = str(post["_id"])
    return {"liked_posts": liked_posts}


@router.get("/lyrics", response_model=List[LyricsInDB])
async def get_user_lyrics(current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    user_id = await get_user_id(current_user.username)
    user_lyrics = await lyrics_collection.find({"user_id": user_id}).to_list(None)
    for lyric in user_lyrics:
        lyric["_id"] = str(lyric["_id"])
    return user_lyrics
