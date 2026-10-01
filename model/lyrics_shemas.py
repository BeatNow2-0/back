from typing import Optional

from bson import ObjectId
from pydantic import BaseModel, Field, field_validator

class NewLyrics(BaseModel):
    title: str = Field(alias="title")
    lyrics: str = Field(alias="lyrics")
    post_id: Optional[str] = Field(default=None, alias="post_id")
    
class Lyrics(NewLyrics):
    user_id: str = Field(alias="user_id")
    
class LyricsInDB(Lyrics):
    id: str = Field(default=None,alias='_id')

    @field_validator('id', mode='before')
    @classmethod
    def convert_id(cls, v):
        return str(v) if isinstance(v, ObjectId) else v
