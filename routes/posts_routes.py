from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile

from config.db import get_database, interactions_collection, lyrics_collection, post_collection, users_collection
from config.security import get_current_user, get_user, get_user_id, get_username
from core.mongo import parse_object_id
from model.post_shemas import NewPost, PostInDB, PostShowed, PostUpdate
from model.user_shemas import CurrentUser
from routes.interactions_routes import has_liked_post, has_saved_post
from services.storage import delete_post_directory, prepare_beat_media, with_post_media_urls

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
    return with_post_media_urls(post)


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

    media_batch = None
    try:
        media_batch = await prepare_beat_media(post_id, cover_file, audio_file)
        media_batch.apply()
        update_result = await post_collection.update_one(
            {"_id": result.inserted_id},
            {"$set": media_batch.database_fields},
        )
        if update_result.matched_count == 0:
            raise RuntimeError("Post disappeared while attaching media")
    except Exception:
        if media_batch is not None:
            media_batch.rollback()
        await post_collection.delete_one({"_id": result.inserted_id})
        delete_post_directory(user_id, post_id)
        raise
    media_batch.finalize()

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
    post_object_id = parse_object_id(post_id, field="post identifier")
    post = await post_collection.find_one({"_id": post_object_id})
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    user_id = await get_user_id(current_user.username)
    if user_id != post["user_id"]:
        raise HTTPException(status_code=403, detail="You are not authorized to update this publication")

    update_payload = PostUpdate(
        **{
            "title": title,
            "description": description,
            "genre": genre,
            "tags": _parse_optional_list(tags),
            "moods": _parse_optional_list(moods),
            "instruments": _parse_optional_list(instruments),
            "bpm": bpm,
        }
    )
    update_data = update_payload.model_dump(exclude_none=True)
    media_batch = await prepare_beat_media(
        post_id,
        cover_file,
        audio_file,
        old_cover_key=post.get("cover_key"),
        old_audio_key=post.get("audio_key"),
    )
    update_data.update(media_batch.database_fields)

    try:
        media_batch.apply()
        if update_data:
            update_result = await post_collection.update_one({"_id": post_object_id}, {"$set": update_data})
            if update_result.matched_count == 0:
                raise RuntimeError("Post disappeared while updating")
    except Exception:
        media_batch.rollback()
        raise
    media_batch.finalize()

    updated_post = await post_collection.find_one({"_id": post_object_id})
    return PostInDB(**_with_media_urls(updated_post))


@router.get("/user/{username}", response_model=list[PostInDB])
async def get_posts_for_user(
    username: str,
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    current_user: CurrentUser = Depends(get_current_user),
):
    user = await get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    posts = await post_collection.find({"user_id": user.id}).sort("publication_date", -1).skip(skip).limit(limit).to_list(length=limit)
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
    if not sampled_posts:
        return []
    current_user_id = await get_user_id(current_user.username)
    sampled_ids = [str(post["_id"]) for post in sampled_posts]
    interactions = await interactions_collection.find(
        {"user_id": current_user_id, "post_id": {"$in": sampled_ids}}
    ).to_list(length=len(sampled_ids))
    interaction_map = {str(item["post_id"]): item for item in interactions}
    owner_ids = {str(post.get("user_id")) for post in sampled_posts if post.get("user_id")}
    owner_object_ids = [parse_object_id(owner_id, field="user identifier") for owner_id in owner_ids]
    owners = await users_collection.find(
        {"_id": {"$in": owner_object_ids}}, {"username": 1}
    ).to_list(length=len(owner_object_ids))
    usernames = {str(owner["_id"]): owner.get("username") for owner in owners}
    feed_posts = []
    for post in sampled_posts[:safe_limit]:
        post_id = str(post["_id"])
        interaction = interaction_map.get(post_id, {})
        feed_posts.append(
            PostShowed(
                **_with_media_urls(post),
                creator_username=usernames.get(str(post.get("user_id"))),
                isLiked=interaction.get("like_date") is not None,
                isSaved=interaction.get("saved_date") is not None,
            )
        )
    return feed_posts


@router.get("/{post_id}", response_model=PostShowed)
async def read_publication(post_id: str, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    readed_post = await read_post(post_id, current_user)
    if readed_post is None:
        raise HTTPException(status_code=404, detail="Publication not found")
    return readed_post


async def read_post(post_id: str, current_user: CurrentUser):
    post_dict = await post_collection.find_one({"_id": parse_object_id(post_id, field="post identifier")})
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
    post_object_id = parse_object_id(post_id, field="post identifier")
    existing_publication = await post_collection.find_one({"_id": post_object_id})
    if not existing_publication:
        raise HTTPException(status_code=404, detail="Publication not found")
    user_id = await get_user_id(current_user.username)
    if existing_publication["user_id"] != user_id:
        raise HTTPException(status_code=403, detail="You are not authorized to delete this publication")
    await interactions_collection.delete_many({"post_id": post_id})
    await lyrics_collection.update_many({"post_id": post_id}, {"$set": {"post_id": None}})
    await post_collection.delete_one({"_id": post_object_id})
    delete_post_directory(user_id, post_id)
