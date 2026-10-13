from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile
from starlette.concurrency import run_in_threadpool

from config.settings import settings

logger = logging.getLogger(__name__)
ALLOWED_FORMATS = {"wav", "mp3", "flac", "m4a"}
CONTENT_TYPES = {
    "wav": {"audio/wav", "audio/x-wav", "application/octet-stream"},
    "mp3": {"audio/mpeg", "audio/mp3", "application/octet-stream"},
    "flac": {"audio/flac", "audio/x-flac", "application/octet-stream"},
    "m4a": {"audio/mp4", "audio/x-m4a", "application/octet-stream"},
}


@dataclass(slots=True)
class ProcessedBeatAudio:
    staged_original: Path
    staged_preview: Path
    master_key: str
    original_filename: str
    original_sha256: str
    original_size: int
    preview_size: int
    duration: float
    audio_format: str
    mime_type: str
    preview_format: str
    preview_mime_type: str

    @property
    def database_fields(self) -> dict:
        return {
            "master_key": self.master_key,
            "original_filename": self.original_filename,
            "original_sha256": self.original_sha256,
            "original_size": self.original_size,
            "preview_size": self.preview_size,
            "duration": self.duration,
            "audio_format": self.preview_format,
            "preview_key": None,
            "original_audio_format": self.audio_format,
            "mime_type": self.mime_type,
            "preview_mime_type": self.preview_mime_type,
            "processing_status": "ready",
        }


def _run(command: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(command, check=True, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(422, "Audio processing timed out") from exc
    except subprocess.CalledProcessError as exc:
        logger.warning("Beat audio tool failed (exit=%s)", exc.returncode)
        raise HTTPException(415, "Invalid or corrupt audio") from exc
    except FileNotFoundError as exc:
        logger.exception("Beat audio processing dependency unavailable")
        raise HTTPException(503, "Audio processing is temporarily unavailable") from exc


def _probe_audio_metadata(source: Path, extension: str) -> tuple[float, int | None]:
    ffprobe = shutil.which(settings.ffprobe_binary)
    if not ffprobe:
        raise HTTPException(503, "Audio processing is temporarily unavailable")
    probe = _run(
        [ffprobe, "-protocol_whitelist", "file,pipe", "-v", "error", "-show_entries", "format=duration,format_name,bit_rate", "-show_entries", "stream=codec_type,bit_rate", "-of", "json", str(source)],
        min(20, settings.beat_processing_timeout_seconds),
    )
    try:
        metadata = json.loads(probe.stdout)
        duration = float(metadata["format"]["duration"])
        audio_streams = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(415, "Invalid or corrupt audio") from exc
    formats = set(str(metadata.get("format", {}).get("format_name", "")).split(","))
    expected_formats = {"wav": {"wav"}, "mp3": {"mp3"}, "flac": {"flac"}, "m4a": {"mov", "mp4", "m4a"}}[extension]
    if not audio_streams or not formats.intersection(expected_formats):
        raise HTTPException(415, "Audio content does not match its file extension")
    if duration <= 0 or duration > settings.beat_analysis_max_duration_seconds:
        raise HTTPException(422, "Audio duration exceeds the configured maximum")
    raw_bitrate = next((stream.get("bit_rate") for stream in audio_streams if stream.get("bit_rate")), None)
    raw_bitrate = raw_bitrate or metadata.get("format", {}).get("bit_rate")
    try:
        bitrate = int(raw_bitrate) if raw_bitrate else None
    except (TypeError, ValueError):
        bitrate = None
    return duration, bitrate


def probe_audio_file(source: Path, extension: str) -> float:
    return _probe_audio_metadata(source, extension)[0]


def _preview_bitrate(source_bitrate: int | None) -> str:
    configured = settings.preview_audio_bitrate.strip().lower()
    multiplier = 1000 if configured.endswith("k") else 1
    try:
        configured_bps = int(configured.removesuffix("k")) * multiplier
    except ValueError as exc:
        raise HTTPException(503, "Audio preview configuration is invalid") from exc
    target_bps = configured_bps
    if source_bitrate and source_bitrate > 8_000:
        target_bps = min(target_bps, max(8_000, int(source_bitrate * 0.75)))
    return f"{max(8, target_bps // 1000)}k"


def process_audio_file(
    source: Path,
    staged_preview: Path,
    extension: str,
    preview_start: float | None = None,
    preview_end: float | None = None,
) -> tuple[float, str]:
    ffmpeg = shutil.which(settings.ffmpeg_binary)
    if not ffmpeg:
        raise HTTPException(503, "Audio processing is temporarily unavailable")
    duration, source_bitrate = _probe_audio_metadata(source, extension)

    start = 0.0 if preview_start is None else preview_start
    if start < 0 or start >= duration:
        raise HTTPException(422, "preview_start must be within the audio duration")
    end = min(duration, start + settings.preview_duration_seconds) if preview_end is None else preview_end
    if end <= start or end > duration:
        raise HTTPException(422, "preview_end must be after preview_start and within the audio duration")
    preview_duration = end - start

    watermark = settings.beat_watermark_file
    use_watermark = bool(settings.beat_watermark_enabled and watermark and watermark.is_file())
    command = [ffmpeg, "-nostdin", "-v", "error", "-y", "-protocol_whitelist", "file,pipe", "-i", str(source)]
    if use_watermark:
        # Mix a configured audio watermark periodically into the preview only.
        try:
            ffprobe = shutil.which(settings.ffprobe_binary)
            if not ffprobe:
                raise RuntimeError("ffprobe unavailable for watermark validation")
            watermark_probe = _run([ffprobe, "-protocol_whitelist", "file,pipe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(watermark)], 10)
            watermark_valid = any(s.get("codec_type") == "audio" for s in json.loads(watermark_probe.stdout).get("streams", []))
        except Exception:
            if settings.environment == "production":
                raise HTTPException(503, "Audio preview protection is unavailable")
            logger.warning("Configured beat watermark is invalid; development preview has no audio tag")
            watermark_valid = False
        if watermark_valid:
            positions = list(range(0, max(1, int(preview_duration)), max(1, settings.beat_watermark_interval_seconds)))
            filters = []
            labels = ["[base]"]
            filters.append(f"[0:a]atrim=start={start}:end={end},asetpts=PTS-STARTPTS[base]")
            for index, seconds in enumerate(positions, start=1):
                command.extend(["-protocol_whitelist", "file,pipe", "-i", str(watermark)])
                label = f"[wm{index}]"
                filters.append(f"[{index}:a]adelay={seconds * 1000}:all=1,volume=0.35{label}")
                labels.append(label)
            filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:duration=first:normalize=0[outa]")
            command.extend(["-filter_complex", ";".join(filters), "-map", "[outa]"])
    if not use_watermark or not watermark_valid:
        if settings.environment == "production":
            raise HTTPException(503, "Audio preview protection is unavailable")
        logger.warning("Generating development preview without an audio tag")
        command.extend(["-ss", str(start), "-t", str(preview_duration)])
    target_format = "mp3" if settings.preview_format == "mp3" else "ipod"
    command.extend(["-vn", "-codec:a", "libmp3lame" if target_format == "mp3" else "aac", "-b:a", _preview_bitrate(source_bitrate), "-f", target_format, str(staged_preview)])
    _run(command, settings.beat_processing_timeout_seconds)
    if not staged_preview.is_file() or staged_preview.stat().st_size == 0:
        raise HTTPException(422, "Audio preview could not be generated")
    return duration, "audio/mpeg" if settings.preview_format == "mp3" else "audio/mp4"


async def prepare_uploaded_audio(
    upload: UploadFile,
    producer_id: str,
    preview_start: float | None = None,
    preview_end: float | None = None,
) -> ProcessedBeatAudio:
    raw_filename = upload.filename or "audio"
    if "/" in raw_filename or "\\" in raw_filename:
        raise HTTPException(415, "Invalid audio filename")
    filename = Path(raw_filename).name
    extension = Path(filename).suffix.lower().lstrip(".")
    if extension not in ALLOWED_FORMATS:
        raise HTTPException(415, "Unsupported audio format")
    if upload.content_type and upload.content_type not in CONTENT_TYPES[extension]:
        raise HTTPException(415, "Unsupported audio MIME type")

    root = settings.beats_original_dir
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    staging = Path(tempfile.mkdtemp(prefix=".processing-", dir=root))
    try:
        original_path = staging / f"source.{extension}"
        digest = hashlib.sha256()
        size = 0
        with original_path.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_audio_upload_size:
                    raise HTTPException(413, "Audio too large")
                digest.update(chunk)
                output.write(chunk)
        if size == 0:
            raise HTTPException(415, "Empty audio upload")
        asset_id = uuid4().hex
        staged_original = staging / f"{asset_id}.{extension}"
        original_path.replace(staged_original)
        staged_original.chmod(0o600)
        preview_path = staging / f"{asset_id}.{settings.preview_format}"
        duration, preview_mime_type = await run_in_threadpool(
            process_audio_file, staged_original, preview_path, extension, preview_start, preview_end
        )
        return ProcessedBeatAudio(
            staged_original=staged_original,
            staged_preview=preview_path,
            master_key=f"{producer_id}/{asset_id}.{extension}",
            original_filename=filename,
            original_sha256=digest.hexdigest(),
            original_size=size,
            preview_size=preview_path.stat().st_size,
            duration=round(duration, 3),
            audio_format=extension,
            mime_type={"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac", "m4a": "audio/mp4"}[extension],
            preview_format=settings.preview_format,
            preview_mime_type=preview_mime_type,
        )
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
