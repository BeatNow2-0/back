# routes/interactions_routes.py
from fastapi import APIRouter, HTTPException, Depends
from fastapi.encoders import jsonable_encoder
from datetime import datetime
from bson import ObjectId
from pymongo import ReturnDocument
from config.db import get_database, interactions_collection, post_collection
from config.security import get_current_user, get_user_id, check_post_exists
from model.user_shemas import NewUser
from model.post_shemas import Post

router = APIRouter()

# Helper: normalized id (si en tu BD usas ObjectId para post_id, convierte aquí)
def _normalize_post_id(post_id: str):
    # Si tus posts usan ObjectId, descomenta la siguiente línea:
    return ObjectId(post_id)
    # Si usas string en interactions -> devuelve tal cual:
    # return post_id

async def check_interaction_exists(user_id: str, post_id: str, field: str, db):
    """
    Lanza 400 si la interacción ya existe (por ejemplo: like_date ya existe).
    """
    post_key = _normalize_post_id(post_id)
    interaction = await interactions_collection.find_one({"user_id": user_id, "post_id": post_key})
    if interaction and field in interaction and interaction.get(field) is not None:
        raise HTTPException(status_code=400, detail=f"{field} already exists")
    return

async def check_uninteraction_exists(user_id: str, post_id: str, field: str, db):
    """
    Lanza 400 si la interacción NO existe (por ejemplo: intentar quitar un like que no existe).
    """
    post_key = _normalize_post_id(post_id)
    interaction = await interactions_collection.find_one({"user_id": user_id, "post_id": post_key})
    if not interaction or field not in interaction or interaction.get(field) is None:
        raise HTTPException(status_code=400, detail=f"{field} does not exist")
    return

@router.post("/like/{post_id}")
async def add_like(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    # Validar existencia del post
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_interaction_exists(user_id, post_id, "like_date", db)

    post_key = _normalize_post_id(post_id)
    
    # Añadir like en interactions
    result_doc = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_key},
        {"$set": {"like_date": datetime.utcnow()}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    
    if not result_doc:
        raise HTTPException(status_code=500, detail="Failed to add like")
    
    # Incrementar contador de likes en posts
    post_update = await post_collection.update_one(
        {"_id": post_key},
        {"$inc": {"likes": 1}}
    )
    
    if post_update.modified_count == 0:
        # Rollback: eliminar el like que acabamos de añadir
        await interactions_collection.update_one(
            {"user_id": user_id, "post_id": post_key},
            {"$unset": {"like_date": ""}}
        )
        raise HTTPException(status_code=500, detail="Failed to update post likes counter")
    
    # Serializar ObjectId -> string para JSON
    safe_doc = jsonable_encoder(result_doc, custom_encoder={ObjectId: str})
    return {"message": "Like added successfully", "interaction": safe_doc}

@router.delete("/unlike/{post_id}")
async def remove_like(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_uninteraction_exists(user_id, post_id, "like_date", db)

    post_key = _normalize_post_id(post_id)
    
    # Remover like de interactions
    result = await interactions_collection.update_one(
        {"user_id": user_id, "post_id": post_key},
        {"$unset": {"like_date": ""}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to remove like")
    
    # Decrementar contador de likes en posts
    post_update = await post_collection.update_one(
        {"_id": post_key},
        {"$inc": {"likes": -1}}
    )
    
    if post_update.modified_count == 0:
        # Rollback: restaurar el like
        await interactions_collection.update_one(
            {"user_id": user_id, "post_id": post_key},
            {"$set": {"like_date": datetime.utcnow()}}
        )
        raise HTTPException(status_code=500, detail="Failed to update post likes counter")
    
    return {"message": "Like removed successfully"}

# Save (guardar post)
@router.post("/save/{post_id}")
async def save_publication(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_interaction_exists(user_id, post_id, "saved_date", db)

    post_key = _normalize_post_id(post_id)
    
    # Añadir save en interactions
    result_doc = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_key},
        {"$set": {"saved_date": datetime.utcnow()}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )
    
    if not result_doc:
        raise HTTPException(status_code=500, detail="Failed to save publication")
    
    # Incrementar contador de saves en posts
    post_update = await post_collection.update_one(
        {"_id": post_key},
        {"$inc": {"saves": 1}}
    )
    
    if post_update.modified_count == 0:
        # Rollback: eliminar el save que acabamos de añadir
        await interactions_collection.update_one(
            {"user_id": user_id, "post_id": post_key},
            {"$unset": {"saved_date": ""}}
        )
        raise HTTPException(status_code=500, detail="Failed to update post saves counter")
    
    safe_doc = jsonable_encoder(result_doc, custom_encoder={ObjectId: str})
    return {"message": "Publication saved successfully", "interaction": safe_doc}

@router.delete("/unsave/{post_id}")
async def remove_saved(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_uninteraction_exists(user_id, post_id, "saved_date", db)

    post_key = _normalize_post_id(post_id)
    
    # Remover save de interactions
    result = await interactions_collection.update_one(
        {"user_id": user_id, "post_id": post_key},
        {"$unset": {"saved_date": ""}}
    )
    
    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to remove saved publication")
    
    # Decrementar contador de saves en posts
    post_update = await post_collection.update_one(
        {"_id": post_key},
        {"$inc": {"saves": -1}}
    )
    
    if post_update.modified_count == 0:
        # Rollback: restaurar el save
        await interactions_collection.update_one(
            {"user_id": user_id, "post_id": post_key},
            {"$set": {"saved_date": datetime.utcnow()}}
        )
        raise HTTPException(status_code=500, detail="Failed to update post saves counter")
    
    return {"message": "Saved publication removed successfully"}

# Contar el número de likes de una publicación
async def count_likes(post_id: str, db=Depends(get_database)):
    try:
        post_key = _normalize_post_id(post_id)
        likes_count = await interactions_collection.count_documents({"post_id": post_key, "like_date": {"$exists": True}})
        return likes_count
    except Exception as e:
        print(f"Error al contar los likes: {e}")
        return 0

# Contar el número de publicaciones guardadas
async def count_saved(post_id: str, db=Depends(get_database)):
    try:
        post_key = _normalize_post_id(post_id)
        saved_count = await interactions_collection.count_documents({"post_id": post_key, "saved_date": {"$exists": True}})
        return saved_count
    except Exception as e:
        print(f"Error al contar los saves: {e}")
        return 0