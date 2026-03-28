from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from bson import ObjectId
from pydantic import BaseModel, ConfigDict, Field, field_validator


class NewPost(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    description: Optional[str] = Field(default=None, max_length=1000)
    tags: Optional[List[str]] = Field(default=None, max_length=20)
    genre: Optional[str] = Field(default=None, max_length=60)
    moods: Optional[List[str]] = Field(default=None, max_length=20)
    instruments: Optional[List[str]] = Field(default=None, max_length=20)
    bpm: Optional[int] = Field(default=None, ge=1, le=400)


class Post(NewPost):
    user_id: str
    publication_date: datetime
    audio_format: str
    cover_format: str
    likes: int = 0
    saves: int = 0
    views: int = 0
    audio_url: Optional[str] = None
    cover_image_url: Optional[str] = None


class PostInDB(Post):
    id: Optional[str] = Field(default=None, alias="_id")
    model_config = ConfigDict(populate_by_name=True)

    @field_validator("id", mode="before")
    @classmethod
    def convert_id(cls, v):
        return str(v) if isinstance(v, ObjectId) else v


class PostShowed(PostInDB):
    creator_username: Optional[str] = None
    isLiked: bool = False
    isSaved: bool = False


class SearchPost(BaseModel):
    genre: Optional[str] = None
    bpm: Optional[int] = None
    mood: Optional[str] = None
    instruments: Optional[List[str]] = None
    key: Optional[str] = None
    tags: Optional[List[str]] = None
    search: Optional[str] = None


class ProfilePost(BaseModel):
    id: str = Field(alias="_id")
    title: str
    description: str
    views: int = 0


class Tag(BaseModel):
    name: str
    description: str


class Genre(BaseModel):
    name: str
    description: str


class Mood(BaseModel):
    name: str
    description: str


class Instrument(BaseModel):
    name: str
    description: str
