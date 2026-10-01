from __future__ import annotations

import difflib
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from config.db import post_collection, users_collection
from config.security import get_current_user, get_user_id
from model.post_shemas import PostInDB
from model.user_shemas import CurrentUser, UserInDB, UserSearch
from services.storage import profile_media_url, with_post_media_urls

try:
    from Levenshtein import distance as levenshtein_distance
except ModuleNotFoundError:
    def levenshtein_distance(a: str, b: str) -> int:
        return int((1 - difflib.SequenceMatcher(None, a, b).ratio()) * max(len(a), len(b), 1))


router = APIRouter()
logger = logging.getLogger(__name__)


def _parse_csv(value: Optional[str]) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _post_payload(post: dict) -> dict:
    return with_post_media_urls(post)


@router.get("/search_posts", response_model=List[PostInDB])
async def search_posts(
    genre: Optional[str] = Query(None),
    moods: Optional[str] = Query(None),
    instruments: Optional[str] = Query(None),
    bpm: Optional[int] = Query(None),
    search: Optional[str] = Query(None),
    query: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    current_user: CurrentUser = Depends(get_current_user),
):
    user_id = await get_user_id(current_user.username)
    mongo_query: dict[str, object] = {"user_id": {"$ne": user_id}}

    if genre:
        mongo_query["genre"] = genre
    if bpm is not None:
        mongo_query["bpm"] = bpm

    mood_values = _parse_csv(moods)
    if mood_values:
        mongo_query["moods"] = {"$in": mood_values}

    instrument_values = _parse_csv(instruments)
    if instrument_values:
        mongo_query["instruments"] = {"$all": instrument_values}

    try:
        results = await post_collection.find(mongo_query).skip(skip).limit(limit).to_list(length=limit)
        term = (search or query or "").strip().lower()

        if term:
            def similarity_score(post: dict) -> tuple[float, float, float]:
                title_similarity = difflib.SequenceMatcher(None, term, str(post.get("title", "")).lower()).ratio()
                tags_similarity = max(
                    (
                        difflib.SequenceMatcher(None, term, str(tag).lower()).ratio()
                        for tag in post.get("tags", [])
                    ),
                    default=0,
                )
                description_similarity = difflib.SequenceMatcher(
                    None,
                    term,
                    str(post.get("description", "")).lower(),
                ).ratio()
                return (title_similarity, tags_similarity, description_similarity)

            results = sorted(results, key=similarity_score, reverse=True)

        return [PostInDB(**_post_payload(document)) for document in results]
    except Exception as exc:
        logger.exception("Post search failed")
        raise HTTPException(status_code=500, detail="Database query failed") from exc


@router.get("/user/", response_model=List[UserInDB])
async def search_user(
    params: UserSearch = Depends(),
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    current_user: CurrentUser = Depends(get_current_user),
):
    escaped_username = re.escape(params.username)
    query = {
        "$and": [
            {"username": {"$ne": current_user.username}},
            {
                "$or": [
                    {"username": {"$regex": f"^{escaped_username}", "$options": "i"}},
                    {"full_name": {"$regex": f"^{escaped_username}", "$options": "i"}} if escaped_username else {},
                ]
            },
        ]
    }

    try:
        cursor = users_collection.find(query).sort("username").skip(skip).limit(limit)
        results = await cursor.to_list(length=limit)
        users = [
            UserInDB(**{**doc, "profile_image_url": profile_media_url(doc)})
            for doc in results
            if "username" in doc
        ]
        return sort_users_by_similarity(params.username, users)
    except Exception as exc:
        logger.exception("User search failed")
        raise HTTPException(status_code=500, detail="Database query failed") from exc


def sort_users_by_similarity(target: str, users: List[UserInDB]) -> List[UserInDB]:
    def similarity_score(user: UserInDB) -> tuple[int, int]:
        username_similarity = levenshtein_distance(target.lower(), user.username.lower())
        fullname_similarity = levenshtein_distance(target.lower(), (user.full_name or "").lower())
        return (username_similarity, fullname_similarity)

    return sorted(users, key=similarity_score)
