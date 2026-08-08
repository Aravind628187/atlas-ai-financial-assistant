"""
Gmail / Calendar / Drive / Sheets integration.

Scoped honestly for a hackathon submission: wiring a real Google OAuth
consent flow requires a Google Cloud project, verified redirect URI, and
credentials only the developer running this bot can generate — those
can't ship inside a template. This module defines the exact contract the
rest of the app expects, with a working OAuth URL builder, so plugging in
real Gmail/Calendar features is a matter of:

  1. Create a Google Cloud project -> OAuth client (Desktop or Web)
  2. Drop GOOGLE_OAUTH_CLIENT_ID / SECRET into .env
  3. Implement `exchange_code_for_tokens` below with `google-auth-oauthlib`
     (left as a clear TODO so it's obvious exactly where it plugs in)
  4. Implement the two example capabilities stubbed below

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
    return settings.google_oauth_enabled


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


# --- TODO (next milestone, once real credentials exist) --------------------
# def exchange_code_for_tokens(code: str) -> dict: ...
# def summarize_recent_emails(access_token: str, company: str) -> str: ...
# def get_upcoming_events(access_token: str) -> list[dict]: ...
