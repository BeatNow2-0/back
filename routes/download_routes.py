from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from config.settings import settings

router = APIRouter()
APK_DIRECTORY = settings.media_root / "android"


@router.get("/android-apk")
async def download_android_apk():
    apk_path = APK_DIRECTORY / "beatnow_app.apk"
    if not apk_path.exists():
        raise HTTPException(status_code=404, detail="APK not found")
    return FileResponse(apk_path, media_type="application/vnd.android.package-archive", filename=apk_path.name)


@router.get("/latest-apk")
async def download_latest_apk():
    apk_files = sorted(APK_DIRECTORY.glob("*.apk"), key=lambda path: path.stat().st_mtime, reverse=True)
    if not apk_files:
        raise HTTPException(status_code=404, detail="No APK found")
    latest_apk = apk_files[0]
    return FileResponse(latest_apk, media_type="application/vnd.android.package-archive", filename=latest_apk.name)
