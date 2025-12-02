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


# ------------- Helper para actualizar post_collection probando ObjectId/string -------------
async def _inc_post_field(post_id: str, field: str, amount: int = 1):
    """
    Incrementa el campo `field` en post_collection probando ambos tipos de _id:
      1) ObjectId(post_id) (si post_id tiene 24 hex chars)
      2) post_id como string
    Devuelve (updated_post_dict_or_None, used_key_type_string_or_None)
    """
    update_doc = {"$inc": {field: amount}}
    # 1) intentar ObjectId si parece hex de 24
    try:
        if isinstance(post_id, str) and len(post_id) == 24:
            try:
                oid = ObjectId(post_id)
                updated = await post_collection.find_one_and_update(
                    {"_id": oid},
                    update_doc,
                    return_document=ReturnDocument.AFTER
                )
                if updated:
                    return updated, "objectid"
            except Exception as e:
                # fallo conversión o query con ObjectId -> seguimos intentando con string
                print(f"[_inc_post_field] intento ObjectId failed for {post_id}: {e}")
    except Exception as e:
        print(f"[_inc_post_field] unexpected error on ObjectId attempt: {e}")

    # 2) intentar con string
    try:
        updated = await post_collection.find_one_and_update(
            {"_id": post_id},
            update_doc,
            return_document=ReturnDocument.AFTER
        )
        if updated:
            return updated, "string"
    except Exception as e:
        print(f"[_inc_post_field] intento string failed for {post_id}: {e}")

    # No se pudo actualizar el post (ni como objectid ni como string)
    return None, None


# ------------- comprobaciones de interacción -------------
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


# -----------------------
# LIKE / UNLIKE
# -----------------------
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

    # 2) incrementar contador de likes en post_collection de forma atómica (intentando objectId/string)
    updated_post, used_key = await _inc_post_field(post_id, "likes", 1)
    if updated_post is None:
        print(f"[add_like] Warning: could not increment likes on post_collection for post_id={post_id}")

    safe_interaction = jsonable_encoder(result_doc, custom_encoder={ObjectId: str})
    resp = {"message": "Like added successfully", "interaction": safe_interaction}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
        resp["used_key"] = used_key
    else:
        resp["note"] = "Post not updated directly (possible _id type mismatch)."
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
    updated_post, used_key = await _inc_post_field(post_id, "likes", -1)
    if updated_post is None:
        print(f"[remove_like] Warning: could not decrement likes on post_collection for post_id={post_id}")

    resp = {"message": "Like removed successfully"}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
        resp["used_key"] = used_key
    else:
        resp["note"] = "Post not updated directly (possible _id type mismatch)."
    return resp


# -----------------------
# SAVE / UNSAVE
# -----------------------
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
    updated_post, used_key = await _inc_post_field(post_id, "saves", 1)
    if updated_post is None:
        print(f"[save_publication] Warning: could not increment saves on post_collection for post_id={post_id}")

    safe_doc = jsonable_encoder(result_doc, custom_encoder={ObjectId: str})
    resp = {"message": "Publication saved successfully", "interaction": safe_doc}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
        resp["used_key"] = used_key
    else:
        resp["note"] = "Post not updated directly (possible _id type mismatch)."
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
    updated_post, used_key = await _inc_post_field(post_id, "saves", -1)
    if updated_post is None:
        print(f"[remove_saved] Warning: could not decrement saves on post_collection for post_id={post_id}")

    resp = {"message": "Saved publication removed successfully"}
    if updated_post:
        resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
        resp["used_key"] = used_key
    else:
        resp["note"] = "Post not updated directly (possible _id type mismatch)."
    return resp


# -----------------------
# Contadores auxiliares
# -----------------------
async def count_likes(post_id: str, db=Depends(get_database)):
    try:
        post_key = _normalize_post_id(post_id)
        likes_count = await interactions_collection.count_documents({"post_id": post_key, "like_date": {"$exists": True}})
        return likes_count
    except Exception as e:
        print(f"Error al contar los likes: {e}")
        return 0


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
        used_key = None
        if should_count_view:
            # 4) incrementamos en post_collection el contador de views (operación atómica)
            updated_post, used_key = await _inc_post_field(post_id, "views", 1)
            if updated_post:
                total_views = updated_post.get("views", None)
            else:
                # fallback: contar por interacciones
                total_views = await interactions_collection.count_documents({"post_id": post_key, "last_view": {"$exists": True}})

        # devolver una respuesta útil
        resp = {"message": "View recorded", "counted": should_count_view}
        if total_views is not None:
            resp["total_views"] = total_views
        if updated_post:
            resp["post"] = jsonable_encoder(updated_post, custom_encoder={ObjectId: str})
            resp["used_key"] = used_key
        else:
            resp["note"] = "Post not updated directly (possible _id type mismatch)."

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

        # intentar ObjectId y string para leer post
        try:
            if isinstance(post_key, str) and len(post_key) == 24:
                try:
                    oid = ObjectId(post_key)
                    post_doc = await post_collection.find_one({"_id": oid})
                    if post_doc and isinstance(post_doc.get("views"), int):
                        return post_doc.get("views", 0)
                except Exception:
                    pass
        except Exception:
            pass

        # fallback con string
        post_doc = await post_collection.find_one({"_id": post_key})
        if post_doc and isinstance(post_doc.get("views"), int):
            return post_doc.get("views", 0)

        # fallback final: contar interacciones
        views_count = await interactions_collection.count_documents({"post_id": post_key, "last_view": {"$exists": True}})
        return views_count
    except Exception as e:
        print(f"Error al contar las views: {e}")
        return 0
