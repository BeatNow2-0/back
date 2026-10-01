from __future__ import annotations

from bson import ObjectId
from fastapi import HTTPException, status


def parse_object_id(value: str, *, field: str = "identifier") -> ObjectId:
    if not ObjectId.is_valid(value):
        raise HTTPException(
            status_code=422,
            detail=f"Invalid {field}",
        )
    return ObjectId(value)
