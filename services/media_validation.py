from __future__ import annotations

import io
import wave
from dataclasses import dataclass

from fastapi import HTTPException, UploadFile, status
from mutagen import MutagenError
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4
from PIL import Image, ImageOps, UnidentifiedImageError

from config.settings import settings

ALLOWED_IMAGE_FORMATS = {"JPEG", "PNG", "WEBP"}


@dataclass(frozen=True, slots=True)
class ValidatedImage:
    data: bytes
    extension: str = "webp"
    content_type: str = "image/webp"


@dataclass(frozen=True, slots=True)
class ValidatedAudio:
    data: bytes
    extension: str
    content_type: str


async def _read_limited(upload: UploadFile, max_bytes: int, label: str) -> bytes:
    data = await upload.read(max_bytes + 1)
    if not data:
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail=f"Empty {label} upload")
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"{label.capitalize()} too large",
        )
    return data


async def validate_image_upload(upload: UploadFile) -> ValidatedImage:
    data = await _read_limited(upload, settings.max_image_upload_size, "image")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            detected_format = (probe.format or "").upper()
            if detected_format not in ALLOWED_IMAGE_FORMATS:
                raise HTTPException(
                    status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                    detail="Unsupported image format",
                )
            probe.verify()

        with Image.open(io.BytesIO(data)) as decoded:
            if decoded.width * decoded.height > settings.max_image_pixels:
                raise HTTPException(
                    status_code=413,
                    detail="Image dimensions are too large",
                )
            decoded.load()
            image = ImageOps.exif_transpose(decoded)
            if image.width > settings.max_image_dimension or image.height > settings.max_image_dimension:
                image.thumbnail(
                    (settings.max_image_dimension, settings.max_image_dimension),
                    Image.Resampling.LANCZOS,
                )
            output_image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            output = io.BytesIO()
            output_image.save(output, format="WEBP", quality=88, method=6)
    except HTTPException:
        raise
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Invalid or corrupt image",
        ) from exc

    return ValidatedImage(data=output.getvalue())


def _validate_wav(data: bytes) -> ValidatedAudio | None:
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    try:
        with wave.open(io.BytesIO(data), "rb") as audio:
            if audio.getnchannels() < 1 or audio.getframerate() < 1 or audio.getnframes() < 1:
                raise wave.Error("WAV contains no audio frames")
            audio.readframes(1)
    except (EOFError, wave.Error) as exc:
        raise HTTPException(status_code=415, detail="Invalid or corrupt audio") from exc
    return ValidatedAudio(data=data, extension="wav", content_type="audio/wav")


def _validate_m4a(data: bytes) -> ValidatedAudio | None:
    if len(data) < 12 or data[4:8] != b"ftyp":
        return None
    try:
        parsed = MP4(io.BytesIO(data))
        if not parsed.info or parsed.info.length <= 0:
            raise MutagenError("M4A contains no audio")
    except (MutagenError, OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="Invalid or corrupt audio") from exc
    return ValidatedAudio(data=data, extension="m4a", content_type="audio/mp4")


def _validate_mp3(data: bytes) -> ValidatedAudio | None:
    is_id3 = data.startswith(b"ID3")
    is_frame = len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0
    if not (is_id3 or is_frame):
        return None
    try:
        parsed = MP3(io.BytesIO(data))
        if not parsed.info or parsed.info.length <= 0:
            raise MutagenError("MP3 contains no audio")
    except (MutagenError, OSError, ValueError) as exc:
        raise HTTPException(status_code=415, detail="Invalid or corrupt audio") from exc
    return ValidatedAudio(data=data, extension="mp3", content_type="audio/mpeg")


async def validate_audio_upload(upload: UploadFile) -> ValidatedAudio:
    data = await _read_limited(upload, settings.max_audio_upload_size, "audio")
    for validator in (_validate_wav, _validate_m4a, _validate_mp3):
        validated = validator(data)
        if validated is not None:
            return validated
    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail="Unsupported or invalid audio format",
    )
