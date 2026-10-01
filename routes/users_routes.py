from __future__ import annotations

import logging

from typing import Annotated, List

from bson import ObjectId
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile, status
from fastapi.security import OAuth2PasswordRequestForm
from pymongo.errors import DuplicateKeyError

from config.db import (
    follows_collection,
    get_database,
    interactions_collection,
    lyrics_collection,
    password_reset_collection,
    post_collection,
    users_collection,
)
from config.security import (
    authenticate_user_credentials,
    consume_refresh_token,
    create_email_verification_token,
    get_email_verification_expires_in,
    get_current_user,
    get_current_user_without_confirmation,
    get_user,
    get_user_id,
    revoke_all_refresh_tokens,
    revoke_refresh_token_value,
    issue_token_pair,
    hash_password,
    normalize_login_username,
)
from config.settings import settings
from core.rate_limit import enforce_rate_limit
from core.mongo import parse_object_id
from model.lyrics_shemas import LyricsInDB
from model.post_shemas import PostInDB
from model.user_shemas import (
    CurrentUser,
    InactiveLoginResponse,
    LoginResponse,
    RefreshTokenRequest,
    RegisterRequest,
    UserProfile,
    UserPublic,
    UserUpdate,
    VerificationRequiredResponse,
)
from routes.mail_routes import send_confirmation_email_to_user
from services.storage import (
    create_user_directories,
    delete_user_directories,
    media_url,
    prepare_avatar,
    profile_media_url,
    storage,
    with_post_media_urls,
)

router = APIRouter()
logger = logging.getLogger(__name__)


def _profile_image_url(user: dict) -> str | None:
    return profile_media_url(user)


def _user_public_payload(user: dict) -> dict:
    payload = dict(user)
    payload["profile_image_url"] = _profile_image_url(user)
    return payload


def _post_payload(post: dict) -> dict:
    return with_post_media_urls(post)


@router.post("/register", response_model=VerificationRequiredResponse, status_code=status.HTTP_201_CREATED)
async def register(user: RegisterRequest, request: Request):
    await enforce_rate_limit(request, f"register:{user.username}", settings.register_rate_limit)
    if await users_collection.find_one({"username": user.username}):
        raise HTTPException(status_code=400, detail="Username already registered")
    if await users_collection.find_one({"email": user.email}):
        raise HTTPException(status_code=400, detail="Email already registered")

    user_dict = user.model_dump()
    user_dict["password"] = hash_password(user.password)
    user_dict["is_active"] = False
    try:
        result = await users_collection.insert_one(user_dict)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=400, detail="Username or email already registered") from exc
    user_id = str(result.inserted_id)
    create_user_directories(user_id)
    created = await users_collection.find_one({"_id": result.inserted_id})
    current_user = CurrentUser(**created)
    if not current_user.is_active:
        try:
            await send_confirmation_email_to_user(current_user)
        except Exception:
            logger.exception("Failed to send confirmation email")
    return VerificationRequiredResponse(
        **_user_public_payload(created),
        verification_token=create_email_verification_token(user_id),
        expires_in=get_email_verification_expires_in(),
    )


@router.delete("/delete", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(current_user: CurrentUser = Depends(get_current_user)):
    user_id = await get_user_id(current_user.username)
    post_ids = []
    async for post in post_collection.find({"user_id": user_id}, {"_id": 1}):
        post_ids.append(post["_id"])

    await follows_collection.delete_many({"user_id_following": user_id})
    await follows_collection.delete_many({"user_id_followed": user_id})
    await lyrics_collection.delete_many({"user_id": user_id})
    await interactions_collection.delete_many({"post_id": {"$in": [str(pid) for pid in post_ids]}})
    await interactions_collection.delete_many({"user_id": user_id})
    await post_collection.delete_many({"user_id": user_id})
    await password_reset_collection.delete_many({"user_id": user_id})
    await revoke_all_refresh_tokens(user_id, current_user.username)
    await users_collection.delete_one({"_id": parse_object_id(user_id, field="user identifier")})
    delete_user_directories(user_id, [str(post_id) for post_id in post_ids])
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users/me", response_model=UserPublic)
async def read_users_me(current_user: Annotated[CurrentUser, Depends(get_current_user_without_confirmation)]):
    return UserPublic(**_user_public_payload(current_user.model_dump(by_alias=True)))


@router.get("/posts/{username}", response_model=List[PostInDB])
async def get_posts_by_username(
    username: str,
    current_user: Annotated[CurrentUser, Depends(get_current_user_without_confirmation)],
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
):
    user = await users_collection.find_one({"username": username}, {"_id": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    posts = await post_collection.find({"user_id": str(user["_id"])}).sort("publication_date", -1).skip(skip).limit(limit).to_list(length=limit)
    return [PostInDB(**_post_payload(post)) for post in posts]


@router.get("/profile/{user_id}", response_model=UserProfile)
async def get_user_profile(
    user_id: str,
    current_user: Annotated[CurrentUser, Depends(get_current_user_without_confirmation)],
):
    user = await users_collection.find_one({"_id": parse_object_id(user_id, field="user identifier")})
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
    user_object_id = parse_object_id(user_id, field="user identifier")
    current_document = await users_collection.find_one({"_id": user_object_id}, {"avatar_key": 1})
    if not current_document:
        raise HTTPException(status_code=404, detail="User not found")
    media_batch = await prepare_avatar(user_id, file, current_document.get("avatar_key"))
    try:
        media_batch.apply()
        result = await users_collection.update_one(
            {"_id": user_object_id},
            {"$set": media_batch.database_fields},
        )
        if result.matched_count == 0:
            raise RuntimeError("User disappeared while updating avatar")
    except Exception:
        media_batch.rollback()
        raise
    media_batch.finalize()
    profile_url = media_url(media_batch.database_fields["avatar_key"])
    return {
        "message": "Profile photo updated",
        "profile_image_url": profile_url,
        "photo_profile": profile_url,
        "image_format": "webp",
    }


@router.delete("/delete_photo_profile")
async def delete_photo_profile(current_user: Annotated[CurrentUser, Depends(get_current_user)]):
    user_id = await get_user_id(current_user.username)
    user_object_id = parse_object_id(user_id, field="user identifier")
    user = await users_collection.find_one({"_id": user_object_id}, {"avatar_key": 1})
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await users_collection.update_one({"_id": user_object_id}, {"$unset": {"avatar_key": ""}})
    avatar_key = user.get("avatar_key") or storage.generate_key("avatars", user_id, "avatar.webp")
    storage.delete(avatar_key)
    return {
        "message": "Profile photo reset",
        "profile_image_url": None,
        "photo_profile": None,
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
        await users_collection.update_one(
            {"_id": parse_object_id(current_user.id, field="user identifier")},
            {"$set": update_data},
        )

    updated_user = await users_collection.find_one(
        {"_id": parse_object_id(current_user.id, field="user identifier")}
    )
    if not updated_user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserPublic(**_user_public_payload(updated_user))


@router.post(
    "/login",
    response_model=LoginResponse,
    responses={status.HTTP_403_FORBIDDEN: {"model": InactiveLoginResponse}},
)
async def login_for_access_token(request: Request, form_data: OAuth2PasswordRequestForm = Depends()):
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


@router.post("/refresh", response_model=LoginResponse)
async def refresh_access_token(payload: RefreshTokenRequest):
    user = await consume_refresh_token(payload.refresh_token)
    access_token, refresh_token = await issue_token_pair(user)
    return LoginResponse(access_token=access_token, refresh_token=refresh_token)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(payload: RefreshTokenRequest):
    await revoke_refresh_token_value(payload.refresh_token)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/saved-posts")
async def get_saved_posts(
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    current_user: CurrentUser = Depends(get_current_user),
):
    user_id = await get_user_id(current_user.username)
    saved_posts = await interactions_collection.find({"user_id": user_id, "saved_date": {"$exists": True}}).sort("saved_date", -1).skip(skip).limit(limit).to_list(length=limit)
    valid_post_ids = [post["post_id"] for post in saved_posts if ObjectId.is_valid(post.get("post_id"))]
    object_ids = [parse_object_id(post_id, field="post identifier") for post_id in valid_post_ids]
    original_posts = await post_collection.find({"_id": {"$in": object_ids}}).to_list(length=len(object_ids))
    post_map = {str(post["_id"]): post for post in original_posts}
    owner_ids = {str(post.get("user_id")) for post in original_posts if post.get("user_id")}
    owners = await users_collection.find(
        {"_id": {"$in": [parse_object_id(owner_id, field="user identifier") for owner_id in owner_ids]}},
        {"username": 1},
    ).to_list(length=len(owner_ids))
    username_map = {str(owner["_id"]): owner.get("username") for owner in owners}
    enriched_posts = []
    for post in saved_posts:
        post["_id"] = str(post["_id"])
        original_post = post_map.get(post.get("post_id"))
        if not original_post:
            continue
        creator_id = str(original_post["user_id"])
        post["creator_id"] = creator_id
        post.update({key: value for key, value in with_post_media_urls(original_post).items() if key != "_id"})
        post["creator_username"] = username_map.get(creator_id)
        enriched_posts.append(post)
    return {"saved_posts": enriched_posts}


@router.get("/liked-posts")
async def get_liked_posts(
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    current_user: CurrentUser = Depends(get_current_user),
):
    user_id = await get_user_id(current_user.username)
    liked_posts = await interactions_collection.find({"user_id": user_id, "like_date": {"$exists": True}}).sort("like_date", -1).skip(skip).limit(limit).to_list(length=limit)
    for post in liked_posts:
        post["_id"] = str(post["_id"])
    return {"liked_posts": liked_posts}


@router.get("/lyrics", response_model=List[LyricsInDB])
async def get_user_lyrics(
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    current_user: CurrentUser = Depends(get_current_user),
    db=Depends(get_database),
):
    user_id = await get_user_id(current_user.username)
    user_lyrics = await lyrics_collection.find({"user_id": user_id}).skip(skip).limit(limit).to_list(length=limit)
    for lyric in user_lyrics:
        lyric["_id"] = str(lyric["_id"])
    return user_lyrics
