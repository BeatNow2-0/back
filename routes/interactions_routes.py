# routes/interactions_routes.py
from fastapi import APIRouter, HTTPException, Depends
from fastapi.encoders import jsonable_encoder
from datetime import datetime, timedelta
from bson import ObjectId
from pymongo import ReturnDocument
from config.db import get_database, interactions_collection, post_collection
from config.security import get_current_user, get_user_id, check_post_exists
from model.user_shemas import NewUser

router = APIRouter()


# Helper: normalized id (si en tu BD usas ObjectId para post_id, descomenta)
def _normalize_post_id(post_id: str):
    # Si tus posts usan ObjectId, descomenta la siguiente línea:
    # return ObjectId(post_id)
    # Si usas string en interactions -> devuelve tal cual:
    return post_id


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

    # 1) añadir la interacción (like_date)
    result_doc = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_key},
        {"$set": {"like_date": datetime.utcnow(), "user_id": user_id, "post_id": post_key}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    if not result_doc:
        raise HTTPException(status_code=500, detail="Failed to add like interaction")

    # 2) incrementar contador de likes en post_collection de forma atómica
    try:
        # si tu post._id es ObjectId, asegúrate de que post_key sea ObjectId(post_id)
        updated_post = await post_collection.find_one_and_update(
            {"_id": post_key},
            {"$inc": {"likes": 1}},
            return_document=ReturnDocument.AFTER
        )
    except Exception as e:
        # Log y devolvemos la interacción aunque la actualización del post falle
        print(f"Warning: failed to increment post.likes for {post_id}: {e}")
        updated_post = None

    safe_interaction = jsonable_encoder(result_doc, custom_encoder={ObjectId: str})
    resp = {"message": "Like added successfully", "interaction": safe_interaction}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
    return resp


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
        raise HTTPException(status_code=500, detail="Failed to remove like interaction")

    # decrementar contador de likes en post_collection
    try:
        updated_post = await post_collection.find_one_and_update(
            {"_id": post_key},
            {"$inc": {"likes": -1}},
            return_document=ReturnDocument.AFTER
        )
    except Exception as e:
        print(f"Warning: failed to decrement post.likes for {post_id}: {e}")
        updated_post = None

    resp = {"message": "Like removed successfully"}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
    return resp


# Save (guardar post)
@router.post("/save/{post_id}")
async def save_publication(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    await check_post_exists(post_id, db)
    user_id = await get_user_id(current_user.username)
    await check_interaction_exists(user_id, post_id, "saved_date", db)

    post_key = _normalize_post_id(post_id)
    result_doc = await interactions_collection.find_one_and_update(
        {"user_id": user_id, "post_id": post_key},
        {"$set": {"saved_date": datetime.utcnow(), "user_id": user_id, "post_id": post_key}},
        upsert=True,
        return_document=ReturnDocument.AFTER
    )

    if not result_doc:
        raise HTTPException(status_code=500, detail="Failed to save publication interaction")

    # incrementar contador saves en post_collection
    try:
        updated_post = await post_collection.find_one_and_update(
            {"_id": post_key},
            {"$inc": {"saves": 1}},
            return_document=ReturnDocument.AFTER
        )
    except Exception as e:
        print(f"Warning: failed to increment post.saves for {post_id}: {e}")
        updated_post = None

    safe_doc = jsonable_encoder(result_doc, custom_encoder={ObjectId: str})
    resp = {"message": "Publication saved successfully", "interaction": safe_doc}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
    return resp


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
        raise HTTPException(status_code=500, detail="Failed to remove saved publication interaction")

    # decrementar contador saves en post_collection
    try:
        updated_post = await post_collection.find_one_and_update(
            {"_id": post_key},
            {"$inc": {"saves": -1}},
            return_document=ReturnDocument.AFTER
        )
    except Exception as e:
        print(f"Warning: failed to decrement post.saves for {post_id}: {e}")
        updated_post = None

    resp = {"message": "Saved publication removed successfully"}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
    return resp


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


# ---------------------------
# VIEWS: registrar vista por usuario + incrementar contador en post_collection
# ---------------------------
VIEW_DEDUP_SECONDS = 30  # umbral para no contar vistas muy seguidas del mismo usuario (ajustable)


@router.post("/view/{post_id}")
async def add_view(post_id: str, current_user: NewUser = Depends(get_current_user), db=Depends(get_database)):
    """
    Registra que el usuario ha visto el post.
    - Evita contar vistas repetidas del mismo usuario dentro de VIEW_DEDUP_SECONDS.
    - Actualiza interactions_collection con last_view (y opcionalmente un array views).
    - Incrementa post_collection.views de forma atómica cuando corresponde.
    """
    # 1) validar que el post existe
    await check_post_exists(post_id, db)  # si no existe, esto lanza excepción
    user_id = await get_user_id(current_user.username)
    post_key = _normalize_post_id(post_id)
    now = datetime.utcnow()

    try:
        # 2) obtener la interacción existente (si existe)
        interaction = await interactions_collection.find_one({"user_id": user_id, "post_id": post_key})

        should_count_view = False

        if not interaction:
            # no existía interacción previa: la vista se cuenta
            should_count_view = True
        else:
            last_view = interaction.get("last_view")
            if not last_view:
                should_count_view = True
            else:
                # si la última vista fue hace más de VIEW_DEDUP_SECONDS -> contamos otra
                if isinstance(last_view, datetime):
                    delta = now - last_view
                    if delta.total_seconds() > VIEW_DEDUP_SECONDS:
                        should_count_view = True
                else:
                    # si last_view viene en otro formato, contamos por seguridad
                    should_count_view = True

        # 3) siempre actualizamos el documento de interacción con el último timestamp
        # guardamos last_view y opcionalmente push a array "views" con el timestamp
        # (usar $push puede llevar a arrays grandes; si no lo necesitas, puedes comentar ese $push)
        await interactions_collection.update_one(
            {"user_id": user_id, "post_id": post_key},
            {
                "$set": {"last_view": now, "user_id": user_id, "post_id": post_key},
                "$push": {"views": now}  # opcional: historial por usuario
            },
            upsert=True
        )

        total_views = None
        updated_post = None
        if should_count_view:
            # 4) incrementamos en post_collection el contador de views (operación atómica)
            # Si tu post._id es ObjectId, asegúrate de usar ObjectId() en post_key
            updated_post = await post_collection.find_one_and_update(
                {"_id": post_key},
                {"$inc": {"views": 1}},
                return_document=ReturnDocument.AFTER
            )
            # Si no usas campo views en post_collection, podrías contar desde interactions_collection
            # total_views = await interactions_collection.count_documents({"post_id": post_key, "last_view": {"$exists": True}})
            if updated_post:
                total_views = updated_post.get("views", None)
            else:
                # si por alguna razón no actualizamos post_collection, intenta leer contador por interacciones
                total_views = await interactions_collection.count_documents({"post_id": post_key, "last_view": {"$exists": True}})

        # devolver una respuesta útil
        resp = {"message": "View recorded", "counted": should_count_view}
        if total_views is not None:
            resp["total_views"] = total_views
        if updated_post:
            resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})

        return resp

    except Exception as e:
        # manejo de errores más claro
        print(f"Error al registrar view para post {post_id}: {e}")
        raise HTTPException(status_code=500, detail="Failed to register view")


# Helper: leer contador de views (desde post_collection preferentemente)
async def count_views(post_id: str, db=Depends(get_database)):
    """
    Devuelve el número total de views para un post.
    - Preferimos leer el campo 'views' de post_collection si existe.
    - Alternativamente, se puede calcular por interacciones.
    """
    try:
        post_key = _normalize_post_id(post_id)
        post_doc = await post_collection.find_one({"_id": post_key})
        if post_doc and isinstance(post_doc.get("views"), int):
            return post_doc.get("views", 0)
        # fallback: contar interacciones con last_view
        views_count = await interactions_collection.count_documents({"post_id": post_key, "last_view": {"$exists": True}})
        return views_count
    except Exception as e:
        print(f"Error al contar las views: {e}")
        return 0
