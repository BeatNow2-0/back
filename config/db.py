from typing import List, Optional
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.database import Database
from pymongo.errors import PyMongoError
from fastapi import HTTPException
mongo_client = AsyncIOMotorClient('mongodb+srv://hugogarsan:Hgd452004@cluster0.tbqe7bk.mongodb.net/?appName=Cluster0')

db = mongo_client['BeatNow']

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
    return mongo_client['BeatNow']
# Manejador de excepciones para errores de base de datos
async def handle_database_error(exception: PyMongoError):
    raise HTTPException(status_code=500, detail="Database error")

def parse_list(value: Optional[str]) -> Optional[List[str]]:
        if value:
            return value.split(',')
        return None