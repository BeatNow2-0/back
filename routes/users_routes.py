from __future__ import annotations

import logging

import jwt
from typing import Annotated, List

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status
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
from model.post_shemas import PostInDB
from model.user_shemas import CurrentUser, LoginResponse, NewUser, RefreshTokenRequest, UserProfile, UserPublic, UserUpdate
from routes.mail_routes import send_confirmation_email_to_user
from services.storage import create_user_directories, delete_user_directories, reset_profile_photo, save_profile_photo

router = APIRouter()
logger = logging.getLogger(__name__)


def _profile_image_url(user_id: str) -> str:
    return f"{settings.media_base_url.rstrip('/')}/{user_id}/photo_profile/photo_profile.png"


def _user_public_payload(user: dict) -> dict:
    payload = dict(user)
    payload["profile_image_url"] = _profile_image_url(str(user["_id"]))
    return payload


def _post_payload(post: dict) -> dict:
    payload = dict(post)
    post_id = str(payload.get("_id", ""))
    user_id = str(payload.get("user_id", ""))
    cover_format = payload.get("cover_format")
    audio_format = payload.get("audio_format")
    base_url = settings.media_base_url.rstrip("/")
    if post_id and user_id and cover_format:
        payload["cover_image_url"] = f"{base_url}/{user_id}/posts/{post_id}/caratula.{cover_format}"
    if post_id and user_id and audio_format:
        payload["audio_url"] = f"{base_url}/{user_id}/posts/{post_id}/audio.{audio_format}"
    return payload


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
    current_user = CurrentUser(**created)
    if not current_user.is_active:
        try:
            await send_confirmation_email_to_user(current_user)
        except Exception:
            logger.exception("Failed to send confirmation email for user %s", current_user.username)
    return UserPublic(**_user_public_payload(created))


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
    return UserPublic(**_user_public_payload(current_user.model_dump(by_alias=True)))


@router.get("/posts/{username}", response_model=List[PostInDB])
async def get_posts_by_username(
    username: str,
    current_user: Annotated[CurrentUser, Depends(get_current_user_without_confirmation)],
):
    user = await users_collection.find_one({"username": username}, {"_id": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    posts = await post_collection.find({"user_id": str(user["_id"])}).sort("publication_date", -1).to_list(None)
    return [PostInDB(**_post_payload(post)) for post in posts]


@router.get("/profile/{user_id}", response_model=UserProfile)
async def get_user_profile(
    user_id: str,
    current_user: Annotated[CurrentUser, Depends(get_current_user_without_confirmation)],
):
    user = await users_collection.find_one({"_id": ObjectId(user_id)})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    followers = await follows_collection.count_documents({"user_id_followed": user_id})
    following = await follows_collection.count_documents({"user_id_following": user_id})
    post_num = await post_collection.count_documents({"user_id": user_id})
    current_user_id = await get_user_id(current_user.username)
    is_following_user = False
    if current_user_id != user_id:
        is_following_user = (
            await follows_collection.find_one(
                {"user_id_following": current_user_id, "user_id_followed": user_id}
            )
            is not None
        )

    return UserProfile(
        **_user_public_payload(user),
        followers=followers,
        following=following,
        post_num=post_num,
        is_following=is_following_user,
    )


@router.put("/change_photo_profile")
async def change_photo_profile(
    current_user: Annotated[CurrentUser, Depends(get_current_user)],
    file: UploadFile = File(...),
):
    user_id = await get_user_id(current_user.username)
    image_format = await save_profile_photo(user_id, file)
    return {
        "message": "Profile photo updated",
        "profile_image_url": _profile_image_url(user_id),
        "image_format": image_format,
    }


@router.delete("/delete_photo_profile")
async def delete_photo_profile(current_user: Annotated[CurrentUser, Depends(get_current_user)]):
    user_id = await get_user_id(current_user.username)
    reset_profile_photo(user_id)
    return {
        "message": "Profile photo reset",
        "profile_image_url": _profile_image_url(user_id),
    }


@router.put("/users/me", response_model=UserPublic)
async def update_users_me(
    payload: UserUpdate,
    current_user: Annotated[CurrentUser, Depends(get_current_user_without_confirmation)],
):
    update_data = payload.model_dump(exclude_unset=True)
    if not update_data:
        return UserPublic(**_user_public_payload(current_user.model_dump(by_alias=True)))

    normalized_username = update_data.get("username")
    if normalized_username is not None:
        normalized_username = normalized_username.strip()
        if not normalized_username:
            raise HTTPException(status_code=400, detail="Username cannot be empty")
        update_data["username"] = normalized_username
        if normalized_username != current_user.username:
            existing_user = await users_collection.find_one({"username": normalized_username})
            if existing_user and str(existing_user.get("_id")) != current_user.id:
                raise HTTPException(status_code=400, detail="Username already registered")

    normalized_full_name = update_data.get("full_name")
    if normalized_full_name is not None:
        normalized_full_name = normalized_full_name.strip()
        update_data["full_name"] = normalized_full_name or None

    normalized_bio = update_data.get("bio")
    if normalized_bio is not None:
        normalized_bio = normalized_bio.strip()
        update_data["bio"] = normalized_bio or None

    if update_data:
        await users_collection.update_one({"_id": ObjectId(current_user.id)}, {"$set": update_data})

    updated_user = await users_collection.find_one({"_id": ObjectId(current_user.id)})
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserPublic(**_user_public_payload(updated_user))


@router.post("/login", response_model=LoginResponse)
async def login_for_access_token(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
    await enforce_rate_limit(request, f"login:{form_data.username}", settings.login_rate_limit)
    user_dict = await users_collection.find_one({"username": form_data.username})
    if not user_dict or not verify_password(form_data.password, user_dict.get("password", "")):
        raise HTTPException(status_code=400, detail="Incorrect username or password")

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
        creator_id = await get_post_owner_id(post["post_id"])
        post["creator_id"] = creator_id
        original_post = await post_collection.find_one({"_id": ObjectId(post["post_id"])}, {"cover_format": 1, "audio_format": 1})
        if original_post:
            post["cover_format"] = original_post.get("cover_format")
            post["audio_format"] = original_post.get("audio_format")
            if original_post.get("cover_format"):
                post["cover_image_url"] = (
                    f"{settings.media_base_url.rstrip('/')}/{creator_id}/posts/{post['post_id']}/"
                    f"caratula.{original_post['cover_format']}"
                )
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
