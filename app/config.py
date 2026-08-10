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
    telegram_bot_username: str = "ATLASAI2026BOT"

    # --- Gemini ---------------------------------------------------------
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"
    gemini_cooldown_seconds: int = 300

    # --- Database ---------------------------------------------------------
    database_url: str = "sqlite:///./data/atlas.db"

    # --- Optional data sources ---------------------------------------------
    news_api_key: str = ""
    finnhub_api_key: str = ""
    fmp_api_key: str = ""
    twelve_data_api_key: str = ""
    alpha_vantage_api_key: str = ""
    massive_api_key: str = ""
    market_data_provider: str = "yfinance"
    market_data_api_key: str = ""
    secondary_market_data_provider: str = ""
    secondary_market_data_api_key: str = ""
    quote_cache_ttl_seconds: int = 30
    news_cache_ttl_seconds: int = 300
    fundamentals_cache_ttl_seconds: int = 21600
    quote_stale_after_seconds: int = 900
    provider_timeout_seconds: float = 10.0
    provider_max_retries: int = 2
    financial_provider_timeout_seconds: float = 8.0
    financial_provider_max_retries: int = 2
    financial_provider_verify_critical: bool = True
    quote_verification_tolerance_pct: float = 0.5
    pe_verification_tolerance_pct: float = 15.0
    market_cap_verification_tolerance_pct: float = 5.0
    sec_user_agent: str = "AtlasAI/1.0 admin@example.com"
    sec_cache_ttl_seconds: int = 900
    max_watchlist_items: int = 25

    # --- Optional Google OAuth (Gmail / Calendar / Drive / Sheets) ---------
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    google_oauth_redirect_uri: str = "http://localhost:8000/oauth/google/callback"

    # --- Dashboard / API ---------------------------------------------------
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000

    # --- Admin --------------------------------------------------------------
    admin_email: str = "admin@atlasai.com"
    admin_password: str = ""
    secret_key: str = ""

    # --- Scheduling ---------------------------------------------------------
    alert_poll_interval_seconds: int = 300
    default_timezone: str = "Asia/Kolkata"

    @property
    def google_oauth_enabled(self) -> bool:
        return bool(self.google_oauth_client_id and self.google_oauth_client_secret)


settings = Settings()
