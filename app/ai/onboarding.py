"""Deterministic onboarding state machine; setup never depends on Gemini."""
from __future__ import annotations

import re
import unicodedata

from sqlalchemy.orm import Session

from app.ai.memory import upsert_preference
from app.config import settings
from app.models import OnboardingStage, User, WatchlistItem
from app.services.entity_resolution import KNOWN_COMPANIES, resolve_entities


SKIP_WORDS = {
    "skip", "skip this", "skip for now", "later", "not now", "no", "no thanks",
    "nah", "pass", "next",
}
ROLE_ALIASES = {
    "student": "Student",
    "students": "Student",
    "college student": "Student",
    "engineering student": "Student",
    "investor": "Investor",
    "retail investor": "Investor",
    "analyst": "Analyst",
    "financial analyst": "Analyst",
    "founder": "Founder",
    "entrepreneur": "Founder",
    "finance professional": "Finance Professional",
    "financial professional": "Finance Professional",
}
KNOWN_TICKERS = set(KNOWN_COMPANIES.values())
CASHTAG_RE = re.compile(r"\$([A-Za-z]{1,5}(?:[.-][A-Za-z])?)\b")
SIMPLE_TIME_RE = re.compile(r"^\s*(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?\s*$", re.IGNORECASE)
EMBEDDED_TIME_RE = re.compile(
    r"\b(?:at|around|by)\s+(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?\b",
    re.IGNORECASE,
)


WELCOME = (
    "Hey, I'm **Atlas** 👋 — think of me as an analyst who lives in your pocket. "
    "I'll help you track markets, research companies, read documents, and stay ahead "
    "of anything that matters to your portfolio or your day.\n\n"
    "Takes 30 seconds to get me useful — first, what best describes you? "
    "(e.g. investor, analyst, founder, student, finance professional — or just tell me in your own words)"
)
SKIP_HINT = 'You can say "skip", "not now", or "later".'


def start_onboarding() -> str:
    return WELCOME


def _normalized_phrase(text: str) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = re.sub(r"[^\w\s'-]", " ", value.lower(), flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _sanitize_text(text: str, limit: int) -> str:
    value = unicodedata.normalize("NFKC", str(text or ""))
    value = "".join(" " if unicodedata.category(character)[0] == "C" else character for character in value)
    value = re.sub(r"[<>`*_{}\[\]]", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:limit].strip()


def _looks_like_skip(text: str) -> bool:
    return _normalized_phrase(text) in SKIP_WORDS


def normalize_role(text: str) -> str | None:
    """Map common roles and preserve safe unknown descriptions verbatim."""
    if _looks_like_skip(text):
        return None
    cleaned = _sanitize_text(text, 80)
    if not cleaned:
        return None
    return ROLE_ALIASES.get(_normalized_phrase(cleaned), cleaned)


def _extract_tickers(text: str) -> list[str]:
    """Resolve straightforward known companies/tickers without an AI call."""
    if _looks_like_skip(text):
        return []
    symbols = list(resolve_entities(text).symbols)
    symbols = [symbol for symbol in symbols if symbol != "SEC"]
    for raw in CASHTAG_RE.findall(text):
        symbol = raw.upper().replace(".", "-")
        if symbol not in symbols:
            symbols.append(symbol)
    for token in re.findall(r"\b[A-Za-z]{1,5}\b", text):
        symbol = token.upper()
        if symbol in KNOWN_TICKERS and symbol not in symbols:
            symbols.append(symbol)
    return symbols[: settings.max_watchlist_items]


def _add_watchlist_symbols(db: Session, user: User, symbols: list[str]) -> list[str]:
    existing = {item.symbol for item in user.watchlist_items}
    added: list[str] = []
    available = max(0, settings.max_watchlist_items - len(existing))
    for symbol in symbols:
        if symbol in existing or len(added) >= available:
            continue
        db.add(WatchlistItem(user_id=user.id, symbol=symbol))
        existing.add(symbol)
        added.append(symbol)
    return added


def _parse_briefing_hour(text: str) -> int | None:
    """Parse common local-time answers without guessing unusual free text."""
    if _looks_like_skip(text):
        return None
    phrase = _normalized_phrase(text)
    named_hours = {
        "morning": 7,
        "in the morning": 7,
        "before work": 7,
        "before market open": 8,
        "market open": 9,
        "noon": 12,
        "midday": 12,
        "evening": 18,
        "in the evening": 18,
        "after market close": 17,
        "market close": 16,
        "midnight": 0,
    }
    if phrase in named_hours:
        return named_hours[phrase]
    match = SIMPLE_TIME_RE.fullmatch(text) or EMBEDDED_TIME_RE.search(text)
    if not match:
        return None
    hour = int(match.group(1))
    meridiem = (match.group(3) or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        return (hour % 12) + (12 if meridiem == "pm" else 0)
    return hour if 0 <= hour <= 23 else None


def _store_text_preference(db: Session, user: User, key: str, text: str) -> None:
    if _looks_like_skip(text):
        return
    value = _sanitize_text(text, 300)
    if value:
        upsert_preference(db, user, key, value)


def handle_onboarding_turn(db: Session, user: User, text: str) -> str:
    stage = user.onboarding_stage

    if stage == OnboardingStage.NEW.value:
        user.onboarding_stage = OnboardingStage.ASKED_ROLE.value
        db.flush()
        return WELCOME

    if stage == OnboardingStage.ASKED_ROLE.value:
        role = normalize_role(text)
        if role:
            user.role = role
            upsert_preference(db, user, "role", role)
        user.onboarding_stage = OnboardingStage.ASKED_INTERESTS.value
        db.flush()
        greeting = f"Good to meet you, {role}.\n\n" if role else "No problem.\n\n"
        return f"{greeting}Which companies, sectors, or markets do you follow?\n\n{SKIP_HINT}"

    if stage == OnboardingStage.ASKED_INTERESTS.value:
        symbols = _add_watchlist_symbols(db, user, _extract_tickers(text))
        _store_text_preference(db, user, "sector_interest", text)
        user.onboarding_stage = OnboardingStage.ASKED_MONITORING.value
        db.flush()
        confirmation = f"I added {', '.join(symbols)} to your watchlist.\n\n" if symbols else ""
        return f"{confirmation}Anything you'd like me to monitor?\n\n{SKIP_HINT}"

    if stage == OnboardingStage.ASKED_MONITORING.value:
        symbols = _add_watchlist_symbols(db, user, _extract_tickers(text))
        _store_text_preference(db, user, "monitoring_interest", text)
        user.onboarding_stage = OnboardingStage.ASKED_INTELLIGENCE.value
        db.flush()
        confirmation = f"I added {', '.join(symbols)} to your watchlist.\n\n" if symbols else ""
        return (
            f"{confirmation}What type of intelligence matters most to you — market news, earnings, "
            f"SEC filings, company research, or something else?\n\n{SKIP_HINT}"
        )

    if stage == OnboardingStage.ASKED_INTELLIGENCE.value:
        _store_text_preference(db, user, "intelligence_priority", text)
        user.onboarding_stage = OnboardingStage.ASKED_BRIEFING_TIME.value
        db.flush()
        return f"When would you like your daily briefing?\n\nFor example: 7am, 18:00, morning, or skip."

    if stage == OnboardingStage.ASKED_BRIEFING_TIME.value:
        skipped = _looks_like_skip(text)
        hour = _parse_briefing_hour(text)
        user.briefing_hour_local = hour
        if hour is not None:
            upsert_preference(db, user, "briefing_hour_local", str(hour))
        user.onboarding_stage = OnboardingStage.DONE.value
        db.flush()
        if hour is not None:
            schedule_note = f"Your daily briefing is set for {hour:02d}:00 in your configured timezone."
        elif skipped:
            schedule_note = "No daily briefing time was set. You can add one whenever you want."
        else:
            schedule_note = "I couldn't confidently parse that time, so I left it unset. You can set it anytime."
        return (
            f"{schedule_note}\n\nYou're all set. Try asking what's moving, compare two companies, "
            "or send me a report to review."
        )

    # Backward-compatible completion for users paused on the former final step.
    if stage == OnboardingStage.ASKED_INTEGRATIONS.value:
        user.onboarding_stage = OnboardingStage.DONE.value
        db.flush()
        return "You're all set. What would you like to know?"

    return "You're already set up — what can I help you with?"
