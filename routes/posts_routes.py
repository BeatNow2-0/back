from __future__ import annotations

import json
import random
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from config.db import get_database, interactions_collection, lyrics_collection, post_collection
from config.security import get_current_user, get_user, get_user_id, get_username
from config.settings import settings
from model.post_shemas import NewPost, Post, PostInDB, PostShowed
from model.user_shemas import CurrentUser
from routes.interactions_routes import count_likes, count_saved, has_liked_post, has_saved_post
from services.storage import delete_post_directory, save_post_files, update_post_files

router = APIRouter()


def _parse_optional_list(raw_value: Optional[str]) -> Optional[list[str]]:
    if raw_value is None:
        return None

    value = raw_value.strip()
    if not value:
        return None

    if value.startswith("["):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                normalized = [str(item).strip() for item in parsed if str(item).strip()]
                return normalized or None
        except json.JSONDecodeError:
            pass

    normalized = [item.strip().strip('"').strip("'") for item in value.split(",") if item.strip()]
    return normalized or None


def _with_media_urls(post: dict) -> dict:
    payload = dict(post)
    user_id = str(payload.get("user_id", ""))
    post_id = str(payload.get("_id", ""))
    base_url = settings.media_base_url.rstrip("/")
    cover_format = payload.get("cover_format")
    audio_format = payload.get("audio_format")
    if user_id and post_id and cover_format:
        payload["cover_image_url"] = f"{base_url}/{user_id}/posts/{post_id}/caratula.{cover_format}"
    if user_id and post_id and audio_format:
        payload["audio_url"] = f"{base_url}/{user_id}/posts/{post_id}/audio.{audio_format}"
    return payload


@router.post("/upload", response_model=PostInDB)
async def upload_post(
    cover_file: UploadFile = File(...),
    audio_file: UploadFile = File(...),
    title: str = Form(...),
    description: Optional[str] = Form(None),
    genre: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    moods: Optional[str] = Form(None),
    instruments: Optional[str] = Form(None),
    bpm: Optional[int] = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    new_post = NewPost(
        title=title,
        description=description,
        tags=_parse_optional_list(tags),
        genre=genre,
        moods=_parse_optional_list(moods),
        instruments=_parse_optional_list(instruments),
        bpm=bpm,
    )

    user_id = await get_user_id(current_user.username)
    result = await post_collection.insert_one(
        {
            "user_id": user_id,
            "publication_date": datetime.now(timezone.utc),
            "likes": 0,
            "saves": 0,
            "views": 0,
            **new_post.model_dump(),
            "audio_format": "pending",
            "cover_format": "pending",
        }
    )
    post_id = str(result.inserted_id)

    try:
        cover_format, audio_format = await save_post_files(user_id, post_id, cover_file, audio_file)
        await post_collection.update_one(
            {"_id": result.inserted_id},
            {"$set": {"cover_format": cover_format, "audio_format": audio_format}},
        )
    except Exception:
        await post_collection.delete_one({"_id": result.inserted_id})
        delete_post_directory(user_id, post_id)
        raise

    existing_post = await post_collection.find_one({"_id": result.inserted_id})
    return PostInDB(**_with_media_urls(existing_post))


@router.put("/update/{post_id}", response_model=PostInDB)
async def update_post(
    post_id: str,
    cover_file: UploadFile | None = File(None),
    audio_file: UploadFile | None = File(None),
    title: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    genre: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    moods: Optional[str] = Form(None),
    instruments: Optional[str] = Form(None),
    bpm: Optional[int] = Form(None),
    current_user: CurrentUser = Depends(get_current_user),
):
    post = await post_collection.find_one({"_id": ObjectId(post_id)})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    user_id = await get_user_id(current_user.username)
    if user_id != post["user_id"]:
        raise HTTPException(status_code=403, detail="You are not authorized to update this publication")

    update_data = {
        key: value
        for key, value in {
            "title": title,
            "description": description,
            "genre": genre,
            "tags": _parse_optional_list(tags),
            "moods": _parse_optional_list(moods),
            "instruments": _parse_optional_list(instruments),
            "bpm": bpm,
        }.items()
        if value is not None
    }

    file_updates = await update_post_files(user_id, post_id, cover_file, audio_file)
    update_data.update(file_updates)

    if update_data:
        await post_collection.update_one({"_id": ObjectId(post_id)}, {"$set": update_data})

    updated_post = await post_collection.find_one({"_id": ObjectId(post_id)})
    return PostInDB(**_with_media_urls(updated_post))


@router.get("/user/{username}", response_model=list[PostInDB])
async def get_posts_for_user(
    username: str,
    current_user: CurrentUser = Depends(get_current_user),
):
    user = await get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    posts = await post_collection.find({"user_id": user.id}).sort("publication_date", -1).to_list(None)
    return [PostInDB(**_with_media_urls(post)) for post in posts]


@router.get("/random", response_model=PostShowed)
async def get_random_publication(current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    post_ids = await post_collection.aggregate([{"$sample": {"size": 1}}]).to_list(length=1)
    if not post_ids:
        raise HTTPException(status_code=404, detail="No publications found")
    return await read_publication(str(post_ids[0]["_id"]), current_user, db)


@router.get("/feed", response_model=list[PostShowed])
async def get_feed_publications(
    limit: int = 8,
    exclude_ids: str | None = None,
    current_user: CurrentUser = Depends(get_current_user),
):
    safe_limit = max(1, min(limit, 20))
    excluded_object_ids: list[ObjectId] = []

    if exclude_ids:
        for raw_id in exclude_ids.split(","):
            raw_id = raw_id.strip()
            if ObjectId.is_valid(raw_id):
                excluded_object_ids.append(ObjectId(raw_id))

    match_stage = {"_id": {"$nin": excluded_object_ids}} if excluded_object_ids else {}
    sample_size = min(max(safe_limit * 3, safe_limit), 60)
    pipeline = []
    if match_stage:
        pipeline.append({"$match": match_stage})
    pipeline.append({"$sample": {"size": sample_size}})

    sampled_posts = await post_collection.aggregate(pipeline).to_list(length=sample_size)
    feed_posts: list[PostShowed] = []

    for post in sampled_posts:
        enriched_post = await read_post(str(post["_id"]), current_user)
        if enriched_post is not None:
            feed_posts.append(enriched_post)
        if len(feed_posts) >= safe_limit:
            break

    return feed_posts


@router.get("/{post_id}", response_model=PostShowed)
async def read_publication(post_id: str, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    readed_post = await read_post(post_id, current_user)
    if readed_post is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    return readed_post


async def read_post(post_id: str, current_user: CurrentUser):
    post_dict = await post_collection.find_one({"_id": ObjectId(post_id)})
    if not post_dict:
        return None
    creator_name = await get_username(post_dict["user_id"])
    return PostShowed(
        **_with_media_urls(post_dict),
        creator_username=creator_name,
        isLiked=await has_liked_post(post_id, current_user),
        isSaved=await has_saved_post(post_id, current_user),
    )


@router.delete("/{post_id}", status_code=204)
async def delete_publication(post_id: str, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    existing_publication = await post_collection.find_one({"_id": ObjectId(post_id)})
    if not existing_publication:
        raise HTTPException(status_code=404, detail="Publication not found")
    user_id = await get_user_id(current_user.username)
    if existing_publication["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You are not authorized to delete this publication")
    await interactions_collection.delete_many({"post_id": post_id})
    await lyrics_collection.update_many({"post_id": post_id}, {"$set": {"post_id": None}})
    await post_collection.delete_one({"_id": ObjectId(post_id)})
    delete_post_directory(user_id, post_id)
