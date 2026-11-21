"""Application settings and startup validation."""
from __future__ import annotations

from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Environment
    app_env: Literal["development", "test", "production"] = "development"

    # Security / Auth / CORS
    auth_secret: str = "dev-only-insecure-secret-change-me"
    allow_dev_auth_bypass: bool = False
    frontend_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    session_ttl_seconds: int = 86400

    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"
    gemini_max_concurrency: int = 4

    # ElevenLabs
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_authorized_voice_ids: str = ""
    elevenlabs_voice_consent_verified: bool = False
    elevenlabs_max_concurrency: int = 2

    # Replicate
    replicate_api_token: str = ""
    replicate_model_version: str = ""
    replicate_schema_preset: str = "auto"
    replicate_max_concurrency: int = 3

    # Storage & Database
    output_dir: str = "./storage"
    database_path: str = "./storage/studio.db"
    database_url: str = ""
    redis_url: str = ""

    # Production Cadence & Publishing Safety
    publish_mode: str = "private_queue"
    shorts_per_day: int = 2
    long_videos_per_week: int = 2
    default_language: str = "Hinglish"

    # YouTube OAuth & Publishing Safety
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_redirect_uri: str = "http://localhost:8000/api/youtube/oauth/callback"
    youtube_token_file: str = "./storage/tokens/youtube_oauth.json"
    youtube_public_scheduling_enabled: bool = False
    youtube_kill_switch: bool = False
    youtube_daily_quota_limit: int = 10000

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def storage_root(self) -> Path:
        return Path(self.output_dir).resolve()

    @property
    def authorized_voices(self) -> set[str]:
        return {
            v.strip()
            for v in self.elevenlabs_authorized_voice_ids.split(",")
            if v.strip()
        }

    @property
    def cors_origins(self) -> list[str]:
        return [o.strip() for o in self.frontend_origins.split(",") if o.strip()]


settings = Settings()


def validate_cors_config(cfg: Settings) -> list[str]:
    origins = cfg.cors_origins
    if not origins:
        raise RuntimeError("FRONTEND_ORIGINS must contain at least one explicit origin.")
    if "*" in origins:
        raise RuntimeError("Wildcard '*' in FRONTEND_ORIGINS is forbidden when credentials are enabled.")
    for origin in origins:
        parsed = urlparse(origin)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise RuntimeError(f"Invalid CORS origin in FRONTEND_ORIGINS: {origin}")
        if cfg.app_env == "production" and parsed.scheme != "https":
            raise RuntimeError(f"Production CORS origin must use HTTPS: {origin}")
    return origins


def validate_production_settings(cfg: Settings | None = None) -> None:
    """Validate security-critical configuration at startup without printing secret values."""
    c = cfg or settings
    validate_cors_config(c)

    if c.app_env == "production":
        if c.allow_dev_auth_bypass:
            raise RuntimeError("ALLOW_DEV_AUTH_BYPASS must be false in production.")
        if not c.auth_secret or len(c.auth_secret) < 32 or "dev-only" in c.auth_secret:
            raise RuntimeError("AUTH_SECRET must be set to a strong >=32-char secret in production.")
        required_vars = {
            "GEMINI_API_KEY": c.gemini_api_key,
            "ELEVENLABS_API_KEY": c.elevenlabs_api_key,
            "ELEVENLABS_VOICE_ID": c.elevenlabs_voice_id,
            "ELEVENLABS_AUTHORIZED_VOICE_IDS": c.elevenlabs_authorized_voice_ids,
            "REPLICATE_API_TOKEN": c.replicate_api_token,
            "REPLICATE_MODEL_VERSION": c.replicate_model_version,
            "YOUTUBE_CLIENT_ID": c.youtube_client_id,
            "YOUTUBE_CLIENT_SECRET": c.youtube_client_secret,
        }
        missing = [name for name, val in required_vars.items() if not str(val).strip()]
        if missing:
            raise RuntimeError(f"Missing required production environment variables: {', '.join(missing)}")
        if not c.elevenlabs_voice_consent_verified:
            raise RuntimeError("ELEVENLABS_VOICE_CONSENT_VERIFIED must be true in production.")
        yt_parsed = urlparse(c.youtube_redirect_uri)
        if yt_parsed.scheme != "https" or not yt_parsed.netloc:
            raise RuntimeError("YOUTUBE_REDIRECT_URI must use HTTPS in production.")
