import asyncio
import io
import wave

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image

from config.settings import settings
from services.media_validation import validate_audio_upload, validate_image_upload


def _image_upload(image_format: str, filename: str = "client-file.exe") -> UploadFile:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), (20, 40, 60)).save(buffer, format=image_format)
    return UploadFile(filename=filename, file=io.BytesIO(buffer.getvalue()), headers={"content-type": "application/octet-stream"})


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP"])
def test_real_images_are_decoded_and_normalized(image_format):
    validated = asyncio.run(validate_image_upload(_image_upload(image_format, "../../avatar.jpg.exe")))
    assert validated.extension == "webp"
    assert validated.data.startswith(b"RIFF")
    with Image.open(io.BytesIO(validated.data)) as image:
        assert image.format == "WEBP"


@pytest.mark.parametrize("payload", [b"not-a-jpeg", b"\xff\xd8\xffbroken"])
def test_fake_or_corrupt_images_are_rejected(payload):
    upload = UploadFile(filename="photo.jpg", file=io.BytesIO(payload), headers={"content-type": "image/jpeg"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(validate_image_upload(upload))
    assert exc.value.status_code == 415


def test_oversize_image_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_image_upload_size", 4)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(validate_image_upload(_image_upload("PNG")))
    assert exc.value.status_code == 413


def test_excessive_image_dimensions_are_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_image_pixels", 16)
    with pytest.raises(HTTPException) as exc:
        asyncio.run(validate_image_upload(_image_upload("PNG")))
    assert exc.value.status_code == 413


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 80)
    return buffer.getvalue()


def test_real_wav_is_accepted_even_with_wrong_mime():
    upload = UploadFile(filename="../../audio.mp3.exe", file=io.BytesIO(_wav_bytes()), headers={"content-type": "text/plain"})
    validated = asyncio.run(validate_audio_upload(upload))
    assert validated.extension == "wav"


def test_fake_audio_is_rejected():
    upload = UploadFile(filename="audio.mp3", file=io.BytesIO(b"ID3fake"), headers={"content-type": "audio/mpeg"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(validate_audio_upload(upload))
    assert exc.value.status_code == 415


def test_oversize_audio_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "max_audio_upload_size", 8)
    upload = UploadFile(filename="audio.wav", file=io.BytesIO(_wav_bytes()), headers={"content-type": "audio/wav"})
    with pytest.raises(HTTPException) as exc:
        asyncio.run(validate_audio_upload(upload))
    assert exc.value.status_code == 413
