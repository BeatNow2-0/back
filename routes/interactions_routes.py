from __future__ import annotations

from datetime import datetime, timedelta, timezone

from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException
from fastapi.encoders import jsonable_encoder
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from config.db import get_database, interactions_collection, post_collection
from config.security import check_post_exists, get_current_user, get_user_id
from core.mongo import parse_object_id
from model.user_shemas import CurrentUser

router = APIRouter()
VIEW_DEDUP_SECONDS = 30


def _encoded(document: dict | None) -> dict | None:
    return jsonable_encoder(document, custom_encoder={ObjectId: str}) if document else None


async def _increment_post_counter(post_id: str, field: str, amount: int) -> dict | None:
    query: dict = {"_id": parse_object_id(post_id, field="post identifier")}
    if amount < 0:
        query[field] = {"$gt": 0}
    return await post_collection.find_one_and_update(
        query,
        {"$inc": {field: amount}},
        return_document=ReturnDocument.AFTER,
    )


async def _add_transition(post_id: str, field: str, current_user: CurrentUser) -> dict:
    await check_post_exists(post_id)
    user_id = await get_user_id(current_user.username)
    now = datetime.now(timezone.utc)
    try:
        interaction = await interactions_collection.find_one_and_update(
            {
                "user_id": user_id,
                "post_id": post_id,
                "$or": [{field: {"$exists": False}}, {field: None}],
            },
            {"$set": {field: now, "user_id": user_id, "post_id": post_id}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=400, detail=f"{field} already exists") from exc
    if not interaction:
        raise HTTPException(status_code=400, detail=f"{field} already exists")
    counter = "likes" if field == "like_date" else "saves"
    updated_post = await _increment_post_counter(post_id, counter, 1)
    if not updated_post:
        await interactions_collection.update_one(
            {"user_id": user_id, "post_id": post_id},
            {"$unset": {field: ""}},
        )
        raise HTTPException(status_code=404, detail="Post not found")
    return {
        "message": "Like added successfully" if field == "like_date" else "Publication saved successfully",
        "interaction": _encoded(interaction),
        "post": _encoded(updated_post),
        "used_key": "objectid",
    }


async def _remove_transition(post_id: str, field: str, current_user: CurrentUser) -> dict:
    await check_post_exists(post_id)
    user_id = await get_user_id(current_user.username)
    previous = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_id, field: {"$exists": True, "$ne": None}},
        {"$unset": {field: ""}},
        return_document=ReturnDocument.BEFORE,
    )
    if not previous:
        label = "Like" if field == "like_date" else "Save"
        raise HTTPException(status_code=400, detail=f"{label} does not exist")
    counter = "likes" if field == "like_date" else "saves"
    updated_post = await _increment_post_counter(post_id, counter, -1)
    return {
        "message": "Like removed successfully" if field == "like_date" else "Saved publication removed successfully",
        "interaction_prev": _encoded(previous),
        "post": _encoded(updated_post),
        "used_key": "objectid",
    }


@router.post("/like/{post_id}")
async def add_like(
    post_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db=Depends(get_database),
):
    return await _add_transition(post_id, "like_date", current_user)


@router.delete("/unlike/{post_id}")
async def remove_like(
    post_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db=Depends(get_database),
):
    return await _remove_transition(post_id, "like_date", current_user)


@router.post("/save/{post_id}")
async def save_publication(
    post_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db=Depends(get_database),
):
    return await _add_transition(post_id, "saved_date", current_user)


@router.delete("/unsave/{post_id}")
async def remove_saved(
    post_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db=Depends(get_database),
):
    return await _remove_transition(post_id, "saved_date", current_user)


@router.post("/view/{post_id}")
async def add_view(
    post_id: str,
    current_user: CurrentUser = Depends(get_current_user),
    db=Depends(get_database),
):
    await check_post_exists(post_id)
    user_id = await get_user_id(current_user.username)
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=VIEW_DEDUP_SECONDS)
    counted = True
    try:
        interaction = await interactions_collection.find_one_and_update(
            {
                "user_id": user_id,
                "post_id": post_id,
                "$or": [
                    {"last_view": {"$exists": False}},
                    {"last_view": {"$lt": cutoff}},
                ],
            },
            {"$set": {"last_view": now, "user_id": user_id, "post_id": post_id}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        interaction = None
        counted = False

    updated_post = await _increment_post_counter(post_id, "views", 1) if counted else None
    response = {"message": "View recorded", "counted": counted}
    if updated_post:
        response.update(
            {
                "total_views": updated_post.get("views", 0),
                "post": _encoded(updated_post),
                "used_key": "objectid",
            }
        )
    return response


async def count_likes(post_id: str, db=Depends(get_database)) -> int:
    parse_object_id(post_id, field="post identifier")
    return await interactions_collection.count_documents({"post_id": post_id, "like_date": {"$exists": True}})


async def count_saved(post_id: str, db=Depends(get_database)) -> int:
    parse_object_id(post_id, field="post identifier")
    return await interactions_collection.count_documents({"post_id": post_id, "saved_date": {"$exists": True}})


async def count_views(post_id: str, db=Depends(get_database)) -> int:
    post = await post_collection.find_one(
        {"_id": parse_object_id(post_id, field="post identifier")},
        {"views": 1},
    )
    return int(post.get("views", 0)) if post else 0


async def has_liked_post(post_id: str, current_user: CurrentUser) -> bool:
    user_id = await get_user_id(current_user.username)
    interaction = await interactions_collection.find_one(
        {"user_id": user_id, "post_id": post_id, "like_date": {"$exists": True, "$ne": None}}
    )
    return interaction is not None


async def has_saved_post(post_id: str, current_user: CurrentUser) -> bool:
    user_id = await get_user_id(current_user.username)
    interaction = await interactions_collection.find_one(
        {"user_id": user_id, "post_id": post_id, "saved_date": {"$exists": True, "$ne": None}}
    )
    return interaction is not None
