import logging
import os
from typing import List, Optional
from fastapi.responses import JSONResponse
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.database import Database
from pymongo.errors import PyMongoError
from fastapi import HTTPException, Request
from dotenv import load_dotenv

# Cargar variables del .env
load_dotenv()

# Leer las variables
MONGO_USER = os.getenv("MONGO_USER")
MONGO_PASSWORD = os.getenv("MONGO_PASSWORD")
MONGO_HOST = os.getenv("MONGO_HOST")
MONGO_DB = os.getenv("MONGO_DB")

# Construir la URI sin exponer la contraseña en el código
MONGODB_URI = (
    f"mongodb+srv://{MONGO_USER}:{MONGO_PASSWORD}"
    f"@{MONGO_HOST}/{MONGO_DB}?retryWrites=true&w=majority"
)

# Crear cliente de MongoDB
mongo_client = AsyncIOMotorClient(MONGODB_URI)
db = mongo_client[MONGO_DB]

# Colecciones
users_collection = db['Users']
post_collection = db['Posts']
interactions_collection = db['Interactions']
lyrics_collection = db['Lyrics']
follows_collection = db['Follows']
genres_collection = db['Genres']
moods_collection = db['Moods']
instruments_collection = db['Instruments']
mail_code_collection = db['MailCode']
password_reset_collection = db['PasswordReset']


async def get_database() -> Database:
    return db

# Manejador de errores
async def handle_database_error(request: Request, exc: PyMongoError):
    logging.exception("Database error: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "Database error "})


def parse_list(value: Optional[str]) -> Optional[List[str]]:
    if value:
        return value.split(',')
    return None
