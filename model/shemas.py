from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class Interaction(BaseModel):
    user_id: str
    post_id: str
    like_date: Optional[datetime] = None
    saved_date: Optional[datetime] = None
    dislike_date: Optional[datetime] = None


class MailCode(BaseModel):
    user_id: str
    code: str
    expires_at: datetime
