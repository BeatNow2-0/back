from model.post_shemas import PostInDB, SearchPost
from typing import List, Optional
from model.user_shemas import NewUser, UserInDB, UserSearch
from config.security import  get_current_user, get_user_id
from config.db import users_collection, db, post_collection
from fastapi import Form, HTTPException, Depends, APIRouter
import difflib

try:
    from Levenshtein import distance as levenshtein_distance
except ModuleNotFoundError:
    def levenshtein_distance(a: str, b: str) -> int:
        return int((1 - difflib.SequenceMatcher(None, a, b).ratio()) * max(len(a), len(b), 1))

# Iniciar router
router = APIRouter()

@router.get("/search_posts", response_model=List[PostInDB])
async def search_posts(
    genre: Optional[str] = Form(None),
    moods: Optional[str] = Form(None),
    instruments: Optional[str] = Form(None),
    bpm: Optional[int] = Form(None),
    search: Optional[str] = Form(None),
    current_user: NewUser = Depends(get_current_user)
):
    user_id = await get_user_id(current_user.username)
    query = {"user_id": {"$ne": user_id}}  # Exclude posts from the current user

    # Add exact match filters for 'genre', 'bpm', 'moods', and 'instruments'
    if genre:
        query["genre"] = genre
    if bpm is not None:
        query["bpm"] = bpm
    if moods:
        query["moods"] = moods
    if instruments:
        query["instruments"] = {"$all": instruments.split(",")}

    user_id = await get_user_id(current_user.username)
    query = {"user_id": {"$ne": user_id}}  # Exclude posts from the current user

    # Add exact match filters for 'genre', 'bpm', 'moods', and 'instruments'
    if genre:
        query["genre"] = genre
    if bpm is not None:
        query["bpm"] = bpm
    if moods:
        query["moods"] = moods
    if instruments:
        query["instruments"] = {"$all": instruments.split(",")}

    try:
        cursor = post_collection.find(query)
        results = [document async for document in cursor]  # Asynchronous list comprehension

        # Convert ObjectId to string for the results
        for document in results:
            document["_id"] = str(document["_id"])

        if search:
            # Sort results based on similarity to 'search' in title, tags, and description
            def similarity_score(post):
                title_similarity = difflib.SequenceMatcher(None, search.lower(), post.get("title", "").lower()).ratio()
                tags_similarity = max([difflib.SequenceMatcher(None, search.lower(), tag.lower()).ratio() for tag in post.get("tags", [])]) if post.get("tags") else 0
                description_similarity = difflib.SequenceMatcher(None, search.lower(), post.get("description", "").lower()).ratio()
                return (title_similarity, tags_similarity, description_similarity)

            results = sorted(results, key=similarity_score, reverse=True)

        return results

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query failed: {str(e)}")






@router.get("/user/", response_model=List[UserInDB])
async def search_user(params: UserSearch = Depends(), current_user: UserInDB = Depends(get_current_user)):
    query = {
        "$and": [
            {"username": {"$ne": current_user.username}},  # Excluye el usuario actual de la búsqueda.
            {"$or": [
                {"username": {"$regex": f"^{params.username}", "$options": "i"}},
                {"full_name": {"$regex": f"^{params.username}", "$options": "i"}} if params.username else {}
            ]}
        ]
    }

    try:
        cursor = users_collection.find(query).sort("username")
        results = await cursor.to_list(length=None)
        listusers = [UserInDB(**doc) for doc in results if "username" in doc]
        return sort_users_by_similarity(params.username, listusers)

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database query failed: {str(e)}")


# Función para ordenar usuarios por similitud de nombre de usuario y nombre completo
def sort_users_by_similarity(target: str, users: List[UserInDB]) -> List[UserInDB]:
    def similarity_score(user: UserInDB) -> tuple:
        # Calcula la similitud entre el target y el username
        username_similarity = levenshtein_distance(target.lower(), user.username.lower())
        # Calcula la similitud entre el target y el full_name
        fullname_similarity = levenshtein_distance(target.lower(), (user.full_name or "").lower())
        # Retorna una tupla que prioriza primero la similitud de username
        return (username_similarity, fullname_similarity)
    
    # Ordena los usuarios primero por username_similarity y luego por fullname_similarity
    users_sorted = sorted(users, key=similarity_score)
    return users_sorted