from __future__ import annotations

import asyncio
import logging
from typing import List, Optional

from fastapi import Request
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.database import Database
from pymongo.errors import PyMongoError

from config.settings import settings

logger = logging.getLogger(__name__)

mongo_client = AsyncIOMotorClient(
    settings.resolved_mongo_uri,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=5000,
)
db = mongo_client[settings.mongo_db]

users_collection = db["Users"]
post_collection = db["Posts"]
interactions_collection = db["Interactions"]
lyrics_collection = db["Lyrics"]
follows_collection = db["Follows"]
genres_collection = db["Genres"]
moods_collection = db["Moods"]
instruments_collection = db["Instruments"]
mail_code_collection = db["MailCode"]
password_reset_collection = db["PasswordReset"]
refresh_tokens_collection = db["RefreshTokens"]


async def get_database() -> Database:
    return db


async def handle_database_error(request: Request, exc: PyMongoError):
    logger.exception("Database error on %s", request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Database error"})


async def ping_database() -> bool:
    try:
        await asyncio.wait_for(mongo_client.admin.command("ping"), timeout=5)
        return True
    except Exception as exc:
        logger.warning("MongoDB ping failed during startup: %s", exc)
        return False


async def ensure_indexes() -> bool:
    if not await ping_database():
        return False
    try:
        await asyncio.wait_for(users_collection.create_index("username", unique=True), timeout=5)
        await asyncio.wait_for(users_collection.create_index("email", unique=True), timeout=5)
        await asyncio.wait_for(post_collection.create_index("user_id"), timeout=5)
        await asyncio.wait_for(interactions_collection.create_index([("user_id", 1), ("post_id", 1)], unique=True), timeout=5)
        await asyncio.wait_for(follows_collection.create_index([("user_id_following", 1), ("user_id_followed", 1)], unique=True), timeout=5)
        await asyncio.wait_for(lyrics_collection.create_index("user_id"), timeout=5)
        await asyncio.wait_for(mail_code_collection.create_index("user_id", unique=True), timeout=5)
        await asyncio.wait_for(password_reset_collection.create_index("token_hash", unique=True), timeout=5)
        await asyncio.wait_for(password_reset_collection.create_index("expires_at", expireAfterSeconds=0), timeout=5)
        await asyncio.wait_for(refresh_tokens_collection.create_index("jti", unique=True), timeout=5)
        await asyncio.wait_for(refresh_tokens_collection.create_index("expires_at", expireAfterSeconds=0), timeout=5)
        return True
    except Exception as exc:
        logger.warning("MongoDB index bootstrap failed: %s", exc)
        return False


def parse_list(value: Optional[str]) -> Optional[List[str]]:
    if value:
        return [item.strip() for item in value.split(",") if item.strip()]
    return None
