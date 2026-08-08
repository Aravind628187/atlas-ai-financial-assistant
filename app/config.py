"""
Central configuration for Atlas AI.

All runtime settings are loaded from environment variables (via a local
.env file). Nothing here should ever hold a real secret — see .env.example.
"""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram -----------------------------------------------------
    telegram_bot_token: str = ""

    # --- Gemini ---------------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # --- Database ---------------------------------------------------------
    database_url: str = "sqlite:///./data/atlas.db"

    # --- Optional data sources ---------------------------------------------
    news_api_key: str = ""

    # --- Optional Google OAuth (Gmail / Calendar / Drive / Sheets) ---------
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/oauth/google/callback"

    # --- Dashboard / API ---------------------------------------------------
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000

    # --- Scheduling ---------------------------------------------------------
    alert_poll_interval_seconds: int = 300
    default_timezone: str = "Asia/Kolkata"

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)


settings = Settings()
