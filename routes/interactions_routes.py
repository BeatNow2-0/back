from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime
from bson import ObjectId
from pymongo import ReturnDocument
from config.db import get_database, interactions_collection, post_collection  # asegúrate que posts_collection esté exportado si lo necesitas
from config.security import get_current_user, get_user_id, check_post_exists
from model.user_shemas import NewUser

router = APIRouter()


# Helper: normalized id (si en tu BD usas ObjectId para post_id, convierte aquí)
def _normalize_post_id(post_id: str):
    # Si tus posts usan ObjectId, descomenta la siguiente línea:
    # return ObjectId(post_id)
    # Si usas string en interactions -> devuelve tal cual:
    return post_id


async def check_interaction_exists(user_id: str, post_id: str, field: str, db):
    post_key = _normalize_post_id(post_id)
    interaction = await interactions_collection.find_one({"user_id": user_id, "post_id": post_key})
    if interaction and field in interaction and interaction.get(field) is not None:
        raise HTTPException(status_code=400, detail=f"{field} already exists")
    return


async def check_uninteraction_exists(user_id: str, post_id: str, field: str, db):
    post_key = _normalize_post_id(post_id)
    interaction = await interactions_collection.find_one({"user_id": user_id, "post_id": post_key})
    if not interaction or field not in interaction or interaction.get(field) is None:
        raise HTTPException(status_code=400, detail=f"{field} does not exist")
    return


@router.post("/like/{post_id}")
async def add_like(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    # Ahora pasamos db a check_post_exists
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_interaction_exists(user_id, post_id, "like_date", db)

    post_key = _normalize_post_id(post_id)
    result_doc = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_key},
        {"$set": {"like_date": datetime.utcnow()}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    if not result_doc:
        raise HTTPException(status_code=500, detail="Failed to add like")
    return {"message": "Like added successfully", "interaction": result_doc}



@router.delete("/unlike/{post_id}")
async def remove_like(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_uninteraction_exists(user_id, post_id, "like_date", db)

    post_key = _normalize_post_id(post_id)
    result = await interactions_collection.update_one(
        {"user_id": user_id, "post_id": post_key},
        {"$unset": {"like_date": ""}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to remove like")
    return {"message": "Like removed successfully"}


# Save (guardar post)
@router.post("/save/{post_id}")
async def save_publication(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_interaction_exists(user_id, post_id, "saved_date", db)

    post_key = _normalize_post_id(post_id)
    result_doc = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_key},
        {"$set": {"saved_date": datetime.utcnow()}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    if not result_doc:
        raise HTTPException(status_code=500, detail="Failed to save publication")
    return {"message": "Publication saved successfully", "interaction": result_doc}


@router.delete("/unsave/{post_id}")
async def remove_saved(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_uninteraction_exists(user_id, post_id, "saved_date", db)

    post_key = _normalize_post_id(post_id)
    result = await interactions_collection.update_one(
        {"user_id": user_id, "post_id": post_key},
        {"$unset": {"saved_date": ""}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to remove saved publication")
    return {"message": "Saved publication removed successfully"}


'''# Quitar dislike a publicación
@router.post("/undislike/{post_id}")
async def remove_dislike(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    result = await interactions_collection.update_one(
        {"user_id": user_id, "post_id": post_id},
        {"$unset": {"dislike_date": ""}}
    )
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to remove dislike")
    return {"message": "Dislike removed successfully"}'''

# Contar el número de likes de una publicación
#@router.get("/count/likes/{post_id}")
async def count_likes(post_id: str, db=Depends(get_database)):
    try:
        likes_count = await interactions_collection.count_documents({"post_id": post_id, "like_date": {"$exists": True}})
        return likes_count
    except Exception as e:
        print(f"Error al contar los saves: {e}")
        return 0


# Contar el número de publicaciones guardadas
#@router.get("/count/saved/{post_id}")
async def count_saved(post_id: str, db=Depends(get_database)):
    try:
        saved_count = await interactions_collection.count_documents({"post_id": post_id, "saved_date": {"$exists": True}})
        return saved_count
    except Exception as e:
        print(f"Error al contar los saves: {e}")
        return 0

'''# Contar el número de dislikes de una publicación
#@router.get("/count/dislikes/{post_id}")
async def count_dislikes(post_id: str, db=Depends(get_database)):
    try:
        dislikes_count = await interactions_collection.count_documents({"post_id": post_id, "dislike_date": {"$exists": True}})
        return  dislikes_count
    except Exception as e:
        print(f"Error al contar los saves: {e}")
        return 0'''
'''
@router.get("/protected-route")
async def protected_route(current_user: NewUser = Security(decode_token, scopes=["base"])):
    return {"message": "Hello, secured world!", "user": current_user.username}
'''