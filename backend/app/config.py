from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Gemini
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.5-flash"

    # ElevenLabs
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_authorized_voice_ids: str = ""
    elevenlabs_voice_consent_verified: bool = False

    # Replicate
    replicate_api_token: str = ""
    replicate_model_version: str = ""
    replicate_schema_preset: str = "auto"

    # Storage & App
    output_dir: str = "./storage"
    database_path: str = "./storage/studio.db"
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


settings = Settings()
