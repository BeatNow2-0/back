from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request, status

from config.db import copyright_reports_collection, post_collection
from config.settings import settings
from core.mongo import parse_object_id
from core.rate_limit import enforce_rate_limit, get_client_ip
from model.copyright_schemas import CopyrightReportRequest

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/reports", status_code=status.HTTP_201_CREATED)
async def submit_copyright_report(payload: CopyrightReportRequest, request: Request):
    await enforce_rate_limit(request, "copyright-report", settings.copyright_report_rate_limit)
    email = payload.reporter_email.strip().lower()
    if "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(422, "Invalid reporter email")
    beat_id = parse_object_id(payload.beat_id, field="beat identifier")
    if not await post_collection.find_one({"_id": beat_id}, {"_id": 1}):
        raise HTTPException(404, "Beat not found")
    report_id = str(uuid4())
    now = datetime.now(timezone.utc)
    await copyright_reports_collection.insert_one({
        "_id": report_id,
        "reporter_user_id": None,
        "reporter_email": email,
        "beat_id": payload.beat_id,
        "reason": payload.reason.strip(),
        "details": payload.details.strip(),
        "status": "open",
        "created_at": now,
        "updated_at": now,
        "request_id": getattr(request.state, "request_id", ""),
        "source_ip": get_client_ip(request),
    })
    logger.warning("COPYRIGHT_REPORT", extra={"beat_id": payload.beat_id, "request_id": getattr(request.state, "request_id", "")})
    return {"report_id": report_id, "status": "open", "created_at": now}
