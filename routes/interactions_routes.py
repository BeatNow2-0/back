from fastapi import APIRouter, HTTPException, Depends
from datetime import datetime
from bson import ObjectId
from pymongo import ReturnDocument
from config.db import get_database, interactions_collection, posts_collection  # asegúrate que posts_collection esté exportado si lo necesitas
from config.security import get_current_user, get_user_id, check_post_exists
from model.user_shemas import NewUser

router = APIRouter()


# Helper: normalized id (si en tu BD usas ObjectId para post_id, convierte aquí)
def _normalize_post_id(post_id: str):
    # Si tus posts usan ObjectId, descomenta la siguiente línea:
    # return ObjectId(post_id)
    # Si usas string en interactions -> devuelve tal cual:
    return post_id


async def check_interaction_exists(user_id: str, post_id: str, field: str):
    """Lanza 400 si la interacción ya existe (p.ej. like_date ya existe)."""
    post_key = _normalize_post_id(post_id)
    interaction = await interactions_collection.find_one({"user_id": user_id, "post_id": post_key})
    if interaction and field in interaction and interaction.get(field) is not None:
        raise HTTPException(status_code=400, detail=f"{field} already exists")
    return


async def check_uninteraction_exists(user_id: str, post_id: str, field: str):
    """Lanza 400 si la interacción NO existe (p.ej. intentar quitar un like que no existe)."""
    post_key = _normalize_post_id(post_id)
    interaction = await interactions_collection.find_one({"user_id": user_id, "post_id": post_key})
    if not interaction or field not in interaction or interaction.get(field) is None:
        raise HTTPException(status_code=400, detail=f"{field} does not exist")
    return


# Ruta para añadir like
@router.post("/like/{post_id}")
async def add_like(post_id: str, current_user: NewUser = Depends(get_current_user)):
    await check_post_exists(post_id)  # deja que lance 404 si no existe
    user_id = await get_user_id(current_user.username)
    await check_interaction_exists(user_id, post_id, "like_date")

    post_key = _normalize_post_id(post_id)
    # Usamos find_one_and_update para devolver el documento después del update si queremos
    result_doc = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_key},
        {"$set": {"like_date": datetime.utcnow()}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    # Debug - imprime estado
    # print("DBG like result_doc:", result_doc)

    if not result_doc:
        raise HTTPException(status_code=500, detail="Failed to add like")
    return {"message": "Like added successfully", "interaction": result_doc}


# Ruta para quitar like
@router.delete("/unlike/{post_id}")
async def remove_like(post_id: str, current_user: NewUser = Depends(get_current_user)):
    await check_post_exists(post_id)
    user_id = await get_user_id(current_user.username)
    await check_uninteraction_exists(user_id, post_id, "like_date")

    post_key = _normalize_post_id(post_id)
    result = await interactions_collection.update_one(
        {"user_id": user_id, "post_id": post_key},
        {"$unset": {"like_date": ""}}
    )

    # Debug:
    # print("DBG unlike update result:", result.raw_result)

    if result.modified_count == 0:
        # Si no modificó nada, puede que no existiera (debería haber sido capturado por check_uninteraction_exists)
        raise HTTPException(status_code=500, detail="Failed to remove like")
    return {"message": "Like removed successfully"}


# Save (guardar post)
@router.post("/save/{post_id}")
async def save_publication(post_id: str, current_user: NewUser = Depends(get_current_user)):
    await check_post_exists(post_id)
    user_id = await get_user_id(current_user.username)
    await check_interaction_exists(user_id, post_id, "saved_date")

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


# Unsave
@router.delete("/unsave/{post_id}")
async def remove_saved(post_id: str, current_user: NewUser = Depends(get_current_user)):
    await check_post_exists(post_id)
    user_id = await get_user_id(current_user.username)
    await check_uninteraction_exists(user_id, post_id, "saved_date")

    post_key = _normalize_post_id(post_id)
    result = await interactions_collection.update_one(
        {"user_id": user_id, "post_id": post_key},
        {"$unset": {"saved_date": ""}}
    )

    if result.modified_count == 0:
        raise HTTPException(status_code=500, detail="Failed to remove saved publication")
    return {"message": "Saved publication removed successfully"}
