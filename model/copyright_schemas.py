from __future__ import annotations

from pydantic import BaseModel, Field


class CopyrightReportRequest(BaseModel):
    beat_id: str = Field(min_length=24, max_length=24)
    reporter_email: str = Field(min_length=3, max_length=254)
    reason: str = Field(min_length=3, max_length=80)
    details: str = Field(min_length=10, max_length=5000)
