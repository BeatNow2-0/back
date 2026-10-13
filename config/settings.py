from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv
except ModuleNotFoundError:  # pragma: no cover
    def load_dotenv(*args, **kwargs):
        return False

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = os.getenv("BEATNOW_ENV_FILE", str(BASE_DIR / ".env"))
if Path(DEFAULT_ENV_FILE).exists():
    load_dotenv(DEFAULT_ENV_FILE)


def _split_csv(value: str | None) -> List[str]:
    if not value:
        return []
    normalized: List[str] = []
    for item in value.split(","):
        item = item.strip().rstrip("/")
        if item and item not in normalized:
            normalized.append(item)
    return normalized


@dataclass(slots=True)
class Settings:
    app_name: str = os.getenv("APP_NAME", "BeatNow API")
    environment: str = os.getenv("ENVIRONMENT", "development")
    debug: bool = os.getenv("DEBUG", "false").lower() == "true"

    secret_key: str = os.getenv("SECRET_KEY", "")
    algorithm: str = os.getenv("ALGORITHM", "HS256")
    access_token_expire_minutes: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "15"))
    refresh_token_expire_minutes: int = int(os.getenv("REFRESH_TOKEN_EXPIRE_MINUTES", str(60 * 24 * 7)))
    refresh_cookie_name: str = os.getenv("REFRESH_COOKIE_NAME", "beatnow_refresh")
    refresh_cookie_domain: str | None = os.getenv("REFRESH_COOKIE_DOMAIN") or None
    password_reset_expire_minutes: int = int(os.getenv("PASSWORD_RESET_EXPIRE_MINUTES", "30"))
    confirmation_code_expire_minutes: int = int(os.getenv("CONFIRMATION_CODE_EXPIRE_MINUTES", "10"))
    email_verification_token_expire_minutes: int = int(os.getenv("EMAIL_VERIFICATION_TOKEN_EXPIRE_MINUTES", "10"))

    mongo_user: str = os.getenv("MONGO_USER", "")
    mongo_password: str = os.getenv("MONGO_PASSWORD", "")
    mongo_host: str = os.getenv("MONGO_HOST", "")
    mongo_db: str = os.getenv("MONGO_DB", "BeatNow")
    mongo_uri: str = os.getenv("MONGO_URI", "")

    cors_origins: List[str] = None  # type: ignore[assignment]

    smtp_host: str = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port: int = int(os.getenv("SMTP_PORT", "465"))
    smtp_username: str = os.getenv("SMTP_USERNAME", "")
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    email_sender: str = os.getenv("EMAIL_SENDER", "")

    public_base_url: str = os.getenv("PUBLIC_BASE_URL", "https://api.example.com")
    app_web_base_url: str = os.getenv("APP_WEB_BASE_URL", os.getenv("PUBLIC_BASE_URL", "https://api.example.com"))
    media_base_url: str = os.getenv("MEDIA_BASE_URL", "https://res.beatnow.app")
    media_root: Path = Path(os.getenv("MEDIA_ROOT", "/srv/beatnow/media"))
    beats_original_dir: Path = Path(os.getenv("BEATS_ORIGINAL_DIR", str(Path(os.getenv("MEDIA_ROOT", "/srv/beatnow/media")).parent / "private" / "beats" / "originals")))
    beats_preview_dir: Path = Path(os.getenv("BEATS_PREVIEW_DIR", str(Path(os.getenv("MEDIA_ROOT", "/srv/beatnow/media")) / "previews")))
    preview_duration_seconds: float = float(os.getenv("PREVIEW_DURATION_SECONDS", "30"))
    preview_audio_bitrate: str = os.getenv("PREVIEW_AUDIO_BITRATE", "96k")
    preview_format: str = os.getenv("PREVIEW_FORMAT", "mp3").lower()
    ffmpeg_binary: str = os.getenv("FFMPEG_BINARY", "ffmpeg")
    ffprobe_binary: str = os.getenv("FFPROBE_BINARY", "ffprobe")
    beat_watermark_enabled: bool = os.getenv("BEAT_WATERMARK_ENABLED", "false").lower() == "true"
    beat_watermark_file: Path | None = Path(os.environ["BEAT_WATERMARK_FILE"]) if os.getenv("BEAT_WATERMARK_FILE") else None
    beat_watermark_interval_seconds: int = int(os.getenv("BEAT_WATERMARK_INTERVAL_SECONDS", "30"))
    beat_processing_timeout_seconds: int = int(os.getenv("BEAT_PROCESSING_TIMEOUT_SECONDS", "120"))
    rights_confirmation_version: str = os.getenv("RIGHTS_CONFIRMATION_VERSION", "beta-1")
    copyright_report_rate_limit: int = int(os.getenv("COPYRIGHT_REPORT_RATE_LIMIT", "5"))
    beat_upload_rate_limit: int = int(os.getenv("BEAT_UPLOAD_RATE_LIMIT", "10"))
    beat_stream_rate_limit: int = int(os.getenv("BEAT_STREAM_RATE_LIMIT", "120"))
    search_rate_limit: int = int(os.getenv("SEARCH_RATE_LIMIT", "60"))
    storage_provider: str = os.getenv("STORAGE_PROVIDER", "local")
    max_image_upload_size: int = int(os.getenv("MAX_IMAGE_UPLOAD_SIZE", str(10 * 1024 * 1024)))
    max_audio_upload_size: int = int(os.getenv("MAX_AUDIO_UPLOAD_SIZE", str(50 * 1024 * 1024)))
    beat_analysis_ttl_hours: int = int(os.getenv("BEAT_ANALYSIS_TTL_HOURS", "24"))
    beat_analysis_max_duration_seconds: int = int(os.getenv("BEAT_ANALYSIS_MAX_DURATION_SECONDS", "600"))
    beat_analysis_max_upload_size: int = int(os.getenv("BEAT_ANALYSIS_MAX_UPLOAD_SIZE", str(100 * 1024 * 1024)))
    max_image_dimension: int = int(os.getenv("MAX_IMAGE_DIMENSION", "4096"))
    max_image_pixels: int = int(os.getenv("MAX_IMAGE_PIXELS", "40000000"))
    rate_limit_window_seconds: int = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))
    login_rate_limit: int = int(os.getenv("LOGIN_RATE_LIMIT", "5"))
    register_rate_limit: int = int(os.getenv("REGISTER_RATE_LIMIT", "3"))
    reset_rate_limit: int = int(os.getenv("RESET_RATE_LIMIT", "3"))
    confirmation_rate_limit: int = int(os.getenv("CONFIRMATION_RATE_LIMIT", "5"))
    confirmation_max_attempts: int = int(os.getenv("CONFIRMATION_MAX_ATTEMPTS", "5"))
    confirmation_resend_cooldown_seconds: int = int(os.getenv("CONFIRMATION_RESEND_COOLDOWN_SECONDS", "60"))
    trusted_proxy_ips: List[str] = None  # type: ignore[assignment]
    trusted_hosts: List[str] = None  # type: ignore[assignment]

    prometheus_enabled: bool = os.getenv("PROMETHEUS_ENABLED", "false").lower() == "true"
    prometheus_port: int = int(os.getenv("PROMETHEUS_PORT", "9000"))
    enable_change_stream_sync: bool = os.getenv("ENABLE_CHANGE_STREAM_SYNC", "false").lower() == "true"

    def __post_init__(self) -> None:
        self.cors_origins = _split_csv(
            os.getenv(
                "CORS_ORIGINS",
                "https://app.beatnow.app,https://beatnow.app,https://www.beatnow.app"
                if self.environment == "production"
                else "https://app.beatnow.app,https://beatnow.app,https://www.beatnow.app,http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://localhost:5174",
            )
        )
        self.trusted_proxy_ips = _split_csv(os.getenv("TRUSTED_PROXY_IPS", "127.0.0.1,::1"))
        self.trusted_hosts = _split_csv(os.getenv("TRUSTED_HOSTS", "api.beatnow.app,localhost,127.0.0.1"))
        if self.storage_provider != "local":
            raise RuntimeError(f"Unsupported STORAGE_PROVIDER: {self.storage_provider}")
        if self.preview_format not in {"mp3", "m4a"}:
            raise RuntimeError("PREVIEW_FORMAT must be mp3 or m4a")
        if self.preview_duration_seconds <= 0:
            raise RuntimeError("PREVIEW_DURATION_SECONDS must be greater than zero")
        if self.environment == "production" and (self.debug or "*" in self.cors_origins):
            raise RuntimeError("DEBUG and wildcard CORS origins are not allowed in production")
        if not self.refresh_cookie_name or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in self.refresh_cookie_name):
            raise RuntimeError("REFRESH_COOKIE_NAME contains unsupported characters")
        if self.environment == "production" and any(origin.startswith(("http://localhost", "http://127.0.0.1")) for origin in self.cors_origins):
            raise RuntimeError("Local development origins are not allowed in production CORS")
        if self.algorithm != "HS256":
            raise RuntimeError("Only the HS256 JWT algorithm is supported")
        original_resolved = self.beats_original_dir.resolve()
        media_resolved = self.media_root.resolve()
        if original_resolved == media_resolved or media_resolved in original_resolved.parents or original_resolved in media_resolved.parents:
            raise RuntimeError("BEATS_ORIGINAL_DIR must be outside MEDIA_ROOT")
        try:
            preview_resolved = Path(self.beats_preview_dir).resolve()
            preview_resolved.relative_to(media_resolved)
            if preview_resolved == media_resolved:
                raise RuntimeError("BEATS_PREVIEW_DIR must be a child of MEDIA_ROOT")
        except ValueError as exc:
            raise RuntimeError("BEATS_PREVIEW_DIR must be within MEDIA_ROOT") from exc
        if self.environment == "production":
            if not self.beat_watermark_enabled or not self.beat_watermark_file:
                raise RuntimeError("A preview audio tag is required in production")
            if not self.beat_watermark_file.is_file():
                raise RuntimeError("BEAT_WATERMARK_FILE must be a readable file in production")
            if not self.secret_key or self.secret_key in {"tu_super_secreto", "change_me_with_a_very_long_random_secret"} or "change_me" in self.secret_key.lower():
                raise RuntimeError("SECRET_KEY must be configured with a strong value in production")
            if len(self.secret_key) < 32:
                raise RuntimeError("SECRET_KEY must be at least 32 characters in production")

    @property
    def resolved_mongo_uri(self) -> str:
        if self.mongo_uri:
            return self.mongo_uri
        if not all([self.mongo_user, self.mongo_password, self.mongo_host, self.mongo_db]):
            if self.environment == "test":
                return f"mongodb://localhost:27017/{self.mongo_db}"
            raise RuntimeError("MongoDB settings are incomplete: configure MONGO_URI or Mongo credentials")
        return (
            f"mongodb+srv://{self.mongo_user}:{self.mongo_password}"
            f"@{self.mongo_host}/{self.mongo_db}?retryWrites=true&w=majority"
        )

    @staticmethod
    def generate_secret(length: int = 48) -> str:
        return secrets.token_urlsafe(length)


settings = Settings()
