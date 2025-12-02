from typing import List, Optional
from bson import ObjectId
from fastapi import Query
from pydantic import BaseModel, Field, validator
from datetime import datetime

# Schemas de Publicaciones
class NewPost(BaseModel):
    title: Optional[str] = Field(alias="title")
    description: Optional[str] = Field(alias="description")
    tags: Optional[list] = Field(alias="tags")
    genre: Optional[str] = Field(alias="genre")
    moods: Optional[list] = Field(alias="moods")
    instruments: Optional[list] = Field(alias="instruments")
    bpm: Optional[int] = Field(alias="bpm")
    #key: Optional[str] = Field(alias="key")

class Post(NewPost):
    user_id: str = Field(alias="user_id")
    publication_date: datetime = Field(alias="publication_date")
    audio_format: str = Field(alias="audio_format")
    cover_format: str = Field(alias="cover_format")
    likes: int = Field(alias="likes")
    saves: int = Field(alias="saves")
    views: int = Field(alias="views", default=0)

    @validator("views", pre=True, always=True)
    def normalize_views(cls, v):
        # si no viene, lo colocamos a 0; si viene como string lo convertimos a int
        if v is None:
            return 0
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

class PostInDB(Post):
    id: Optional[str] = Field(default=None, alias='_id')

    @validator('id', pre=True, always=True)
    def convert_id(cls, v):
        return str(v) if isinstance(v, ObjectId) else v

class PostShowed(PostInDB):
    creator_username: Optional[str] = Field(default=None, alias="creator_username")
    isLiked: Optional[bool] = Field(default=False, alias="isLiked")
    isSaved: Optional[bool] = Field(default=False, alias="isSaved")
    
class SearchPost(BaseModel):
    genre: Optional[str] = None
    bpm: Optional[int] = None
    mood: Optional[str] = None
    instruments: Optional[List[str]] =None
    key: Optional[str] = None
    tags: Optional[List[str]] = None
    search: Optional[str] = None

class ProfilePost(BaseModel):
    id: str = Field(alias="_id")
    title: str = Field(alias="title")
    description: str = Field(alias="description")
    views: Optional[int] = Field(default=0, alias="views")

class Tag(BaseModel):
    name: str = Field(alias="name")
    description: str = Field(alias="description")

class Genre(BaseModel):
    name: str = Field(alias="name")
    description: str = Field(alias="description")

class Mood(BaseModel):
    name: str = Field(alias="name")
    description: str = Field(alias="description")

class Instrument(BaseModel):
    name: str = Field(alias="name")
    description: str = Field(alias="description")
