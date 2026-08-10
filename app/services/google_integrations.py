"""
Gmail / Calendar / Drive / Sheets integration.

Scoped honestly: wiring a complete Google OAuth
consent flow requires a Google Cloud project, verified redirect URI, and
credentials only the developer running this bot can generate — those
can't ship inside a template. This module defines the exact contract the
rest of the app expects, with a working OAuth URL builder, so plugging in
real Gmail/Calendar features is a matter of:

  1. Create a Google Cloud project -> OAuth client (Desktop or Web)
  2. Drop GOOGLE_OAUTH_CLIENT_ID / SECRET into .env
  3. Add server-side token exchange and encrypted token persistence
  4. Add explicitly scoped provider operations

Until then, `is_configured()` is False and Atlas tells the user honestly
that this integration isn't set up yet instead of pretending to connect.
"""
from __future__ import annotations

from urllib.parse import urlencode

from app.config import settings

SCOPES = {
    "gmail": "https://www.googleapis.com/auth/gmail.readonly",
    "calendar": "https://www.googleapis.com/auth/calendar",
    "drive": "https://www.googleapis.com/auth/drive.readonly",
    "sheets": "https://www.googleapis.com/auth/spreadsheets.readonly",
}


def is_configured() -> bool:
    """Whether OAuth client credentials exist; this does not imply a working connection flow."""
    return settings.google_oauth_enabled


def is_connection_available() -> bool:
    """Token exchange is intentionally not advertised until it is implemented end to end."""
    return False


def build_consent_url(provider: str, telegram_id: int) -> str | None:
    """Returns a Google OAuth consent URL, or None if OAuth isn't configured yet."""
    if not is_configured() or provider not in SCOPES:
        return None
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": settings.google_oauth_redirect_uri,
        "response_type": "code",
        "scope": SCOPES[provider],
        "access_type": "offline",
        "prompt": "consent",
        "state": f"{provider}:{telegram_id}",
    }
    return f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
