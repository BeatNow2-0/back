from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query
from pymongo.errors import PyMongoError

from config.db import get_database, lyrics_collection, post_collection
from config.security import get_current_user, get_user_id
from core.mongo import parse_object_id
from model.lyrics_shemas import Lyrics, LyricsInDB, NewLyrics
from model.user_shemas import CurrentUser

router = APIRouter()


async def _validate_linked_beat(post_id: str | None, user_id: str) -> None:
    if not post_id:
        return
    beat = await post_collection.find_one(
        {"_id": parse_object_id(post_id, field="post identifier"), "publication_status": "published", "processing_status": "ready", "preview_key": {"$exists": True, "$ne": None}},
        {"user_id": 1, "visibility": 1},
    )
    if not beat or (beat.get("visibility") == "unpublished" and beat.get("user_id") != user_id):
        raise HTTPException(status_code=404, detail="Beat not found")


@router.get("/user", response_model=List[Lyrics])
async def get_user_lyrics(
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    current_user: CurrentUser = Depends(get_current_user),
    db=Depends(get_database),
):
    user_id = await get_user_id(current_user.username)
    try:
        return await lyrics_collection.find({"user_id": user_id}).skip(skip).limit(limit).to_list(length=limit)
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail="Failed to fetch user lyrics") from e


@router.post("/", response_model=LyricsInDB)
async def create_lyrics(new_lyrics: NewLyrics, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    try:
        lyrics_dict = new_lyrics.model_dump()
        lyrics_dict["user_id"] = await get_user_id(current_user.username)
        await _validate_linked_beat(new_lyrics.post_id, lyrics_dict["user_id"])
        result = await lyrics_collection.insert_one(lyrics_dict)
        return LyricsInDB(**lyrics_dict, id=str(result.inserted_id))
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail="Failed to create lyrics") from e


@router.get("/{lyrics_id}", response_model=Lyrics)
async def get_lyrics(lyrics_id: str, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    try:
        lyrics = await lyrics_collection.find_one({"_id": parse_object_id(lyrics_id, field="lyrics identifier"), "user_id": await get_user_id(current_user.username)})
        if not lyrics:
            raise HTTPException(status_code=404, detail="Lyrics not found")
        return lyrics
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail="Failed to fetch lyrics") from e


@router.put("/{lyrics_id}", response_model=LyricsInDB)
async def update_lyrics(lyrics_id: str, updated_lyrics: NewLyrics, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    try:
        user_id = await get_user_id(current_user.username)
        await _validate_linked_beat(updated_lyrics.post_id, user_id)
        lyrics_object_id = parse_object_id(lyrics_id, field="lyrics identifier")
        result = await lyrics_collection.update_one({"_id": lyrics_object_id, "user_id": user_id}, {"$set": updated_lyrics.model_dump()})
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Lyrics not found")
        updated = await lyrics_collection.find_one({"_id": lyrics_object_id, "user_id": user_id})
        return LyricsInDB(**updated, id=lyrics_id)
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail="Failed to update lyrics") from e


@router.delete("/{lyrics_id}", response_model=LyricsInDB)
async def delete_lyrics(lyrics_id: str, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    try:
        user_id = await get_user_id(current_user.username)
        lyrics = await lyrics_collection.find_one_and_delete({"_id": parse_object_id(lyrics_id, field="lyrics identifier"), "user_id": user_id})
        if not lyrics:
            raise HTTPException(status_code=404, detail="Lyrics not found")
        return LyricsInDB(**lyrics, id=str(lyrics_id))
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail="Failed to delete lyrics") from e
