from __future__ import annotations

import asyncio
import logging

from bson import ObjectId

from config.db import interactions_collection, post_collection
from routes.interactions_routes import count_likes, count_saved

logger = logging.getLogger(__name__)


async def watch_changes():
    while True:
        try:
            async with interactions_collection.watch() as stream:
                async for change in stream:
                    if change["operationType"] not in {"insert", "update", "replace", "delete"}:
                        continue
                    post_id = None
                    if change.get("fullDocument"):
                        post_id = change["fullDocument"].get("post_id")
                    elif change.get("documentKey"):
                        interaction = await interactions_collection.find_one({"_id": change["documentKey"]["_id"]})
                        post_id = interaction.get("post_id") if interaction else None
                    if not post_id:
                        continue
                    likes = await count_likes(post_id)
                    saves = await count_saved(post_id)
                    await post_collection.update_one({"_id": ObjectId(post_id)}, {"$set": {"likes": likes, "saves": saves}})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Change stream watcher failed: %s", exc)
            await asyncio.sleep(5)
