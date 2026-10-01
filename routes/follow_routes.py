from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status

from config.db import follows_collection, get_database, users_collection
from config.security import get_current_user, get_user_id
from core.mongo import parse_object_id
from model.follow_shemas import Follow
from model.user_shemas import CurrentUser

router = APIRouter()


@router.post("/follow/{user_id}", response_model=Follow, status_code=status.HTTP_201_CREATED)
async def create_follow(user_id: str, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    user_id_following = await get_user_id(current_user.username)
    if user_id_following == user_id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself")
    if not await users_collection.find_one({"_id": parse_object_id(user_id, field="user identifier")}):
        raise HTTPException(status_code=404, detail="User not found")
    existing_follow = await follows_collection.find_one({"user_id_followed": user_id, "user_id_following": user_id_following})
    if existing_follow:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Already following this user")
    follow_dict = Follow(user_id_followed=user_id, user_id_following=user_id_following, follow_date=datetime.now(timezone.utc)).model_dump()
    await follows_collection.insert_one(follow_dict)
    return follow_dict


@router.delete("/unfollow/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_follow(user_id: str, current_user: CurrentUser = Depends(get_current_user), db=Depends(get_database)):
    user_id_following = await get_user_id(current_user.username)
    result = await follows_collection.delete_one({"user_id_following": user_id_following, "user_id_followed": user_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Follow not found")


async def count_followers(user_id_followed: str, current_user: CurrentUser | None = None, db=Depends(get_database)):
    return {"user_id_followed": user_id_followed, "followers_count": await follows_collection.count_documents({"user_id_followed": user_id_followed})}


async def count_following(user_id_follower: str, current_user: CurrentUser | None = None, db=Depends(get_database)):
    return {"user_id_follower": user_id_follower, "following_count": await follows_collection.count_documents({"user_id_following": user_id_follower})}


async def is_following(user_id: str, db=Depends(get_database), current_user: CurrentUser = Depends(get_current_user)):
    user_id_following = await get_user_id(current_user.username)
    follow = await follows_collection.find_one({"user_id_following": user_id_following, "user_id_followed": user_id})
    return follow is not None


@router.get("/followers/{user_id_followed}")
async def get_followers(
    user_id_followed: str,
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    db=Depends(get_database),
    current_user: CurrentUser = Depends(get_current_user),
):
    target_id = parse_object_id(user_id_followed, field="user identifier")
    if not await users_collection.find_one({"_id": target_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="User not found")
    followers_docs = await follows_collection.find({"user_id_followed": user_id_followed}).skip(skip).limit(limit).to_list(length=limit)
    follower_ids = [parse_object_id(doc["user_id_following"], field="user identifier") for doc in followers_docs]
    followers = await users_collection.find({"_id": {"$in": follower_ids}}, {"password": 0}).to_list(length=limit)
    return {"followers": followers}


@router.get("/following/{user_id_following}")
async def get_following(
    user_id_following: str,
    limit: int = Query(50, ge=1, le=100),
    skip: int = Query(0, ge=0, le=10000),
    db=Depends(get_database),
    current_user: CurrentUser = Depends(get_current_user),
):
    target_id = parse_object_id(user_id_following, field="user identifier")
    if not await users_collection.find_one({"_id": target_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="User not found")
    following_docs = await follows_collection.find({"user_id_following": user_id_following}).skip(skip).limit(limit).to_list(length=limit)
    following_ids = [parse_object_id(doc["user_id_followed"], field="user identifier") for doc in following_docs]
    following = await users_collection.find({"_id": {"$in": following_ids}}, {"password": 0}).to_list(length=limit)
    return {"following": following}
