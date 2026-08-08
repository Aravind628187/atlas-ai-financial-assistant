"""
Onboarding is just a conversation with a small state machine behind it —
no forms, no inline keyboards, matching the brief's requirement that the
whole experience stay natural. Every question can be skipped by typing
things like "skip" / "later" / "not now".
"""
from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.ai.gemini_client import gemini
from app.models import OnboardingStage, User, WatchlistItem
from app.ai.memory import upsert_preference

logger = logging.getLogger("atlas.onboarding")

SKIP_WORDS = {"skip", "later", "not now", "no", "nah", "pass", "next"}


def _looks_like_skip(text: str) -> bool:
    t = text.strip().lower()
    return t in SKIP_WORDS or "skip" in t


WELCOME = (
    "Hey, I'm **Atlas** 👋 — think of me as an analyst who lives in your pocket. "
    "I'll help you track markets, research companies, read documents, and stay ahead "
    "of anything that matters to your portfolio or your day.\n\n"
    "Takes 30 seconds to get me useful — first, what best describes you? "
    "(e.g. investor, analyst, founder, student, finance professional — or just tell me in your own words)"
)


def start_onboarding() -> str:
    return WELCOME


def _extract_role(text: str) -> str | None:
    if _looks_like_skip(text):
        return None
    data = gemini.generate_json(
        f'User described themselves as: "{text}". ',
        system_instruction=(
            'Classify into one short role label (e.g. "Investor", "Analyst", "Founder", '
            '"Student", "Finance Professional", or another 1-3 word label that fits). '
            'Respond ONLY as JSON: {"role": "..."}'
        ),
    )
    return data.get("role")


def _extract_tickers(text: str) -> list[str]:
    if _looks_like_skip(text):
        return []
    data = gemini.generate_json(
        f'User said they follow: "{text}". Extract stock tickers if you can confidently infer them '
        f"from company names (e.g. Apple -> AAPL, Tesla -> TSLA). ",
        system_instruction='Respond ONLY as JSON: {"symbols": ["..."], "raw_interests": ["..."]}',
    )
    return data.get("symbols", [])


def _extract_hour(text: str) -> int | None:
    if _looks_like_skip(text):
        return None
    data = gemini.generate_json(
        f'User said they want their briefing at: "{text}". Convert to a 24-hour UTC hour integer 0-23, '
        f"best-effort guess if they gave a local time without timezone (assume UTC).",
        system_instruction='Respond ONLY as JSON: {"hour_utc": 7}',
    )
    hour = data.get("hour_utc")
    return int(hour) if isinstance(hour, (int, float)) else None


def handle_onboarding_turn(db: Session, user: User, text: str) -> str:
    stage = user.onboarding_stage

    if stage == OnboardingStage.NEW.value:
        user.onboarding_stage = OnboardingStage.ASKED_ROLE.value
        db.flush()
        return WELCOME

    if stage == OnboardingStage.ASKED_ROLE.value:
        role = _extract_role(text)
        if role:
            user.role = role
            upsert_preference(db, user, "role", role)
        user.onboarding_stage = OnboardingStage.ASKED_INTERESTS.value
        db.flush()
        who = f" Good to meet you, {role}." if role else ""
        return (
            f"{who} Which companies, sectors, or markets do you want me watching for you? "
            f"(e.g. \"Tesla, Nvidia, and semiconductors\" — or say skip)"
        )

    if stage == OnboardingStage.ASKED_INTERESTS.value:
        symbols = _extract_tickers(text)
        for sym in symbols:
            exists = any(w.symbol == sym for w in user.watchlist_items)
            if not exists:
                db.add(WatchlistItem(user_id=user.id, symbol=sym))
        if not _looks_like_skip(text):
            upsert_preference(db, user, "sector_interest", text[:300])
        user.onboarding_stage = OnboardingStage.ASKED_BRIEFING_TIME.value
        db.flush()
        confirm = f" Tracking {', '.join(symbols)} for you." if symbols else ""
        return (
            f"Got it.{confirm} When do you want your daily briefing — morning market open, "
            f"before your day starts, evening wrap-up? Give me a rough time (24h UTC is easiest, "
            f"e.g. \"7am\"), or say skip and I'll pick a sensible default."
        )

    if stage == OnboardingStage.ASKED_BRIEFING_TIME.value:
        hour = _extract_hour(text)
        user.briefing_hour_local = hour if hour is not None else 7
        user.onboarding_stage = OnboardingStage.ASKED_INTEGRATIONS.value
        db.flush()
        return (
            "Last thing, totally optional: I can connect to your **Gmail** or **Google Calendar** "
            "to help prep for meetings and catch company mentions in your inbox. Want to link one now, "
            "or skip for now — you can always do it later just by asking."
        )

    if stage == OnboardingStage.ASKED_INTEGRATIONS.value:
        user.onboarding_stage = OnboardingStage.DONE.value
        db.flush()
        note = (
            "No problem, we can connect that anytime — just say \"connect my Gmail\" whenever you're ready.\n\n"
            if _looks_like_skip(text)
            else "Noted — I'll walk you through connecting that the moment you ask for it.\n\n"
        )
        return (
            f"{note}You're all set. Try me with something real — \"what's moving Nvidia today\", "
            f"\"compare Apple and Microsoft\", or just drop a report and ask me to summarize it. "
            f"I'm listening 🎧"
        )

    return "You're already set up — what can I help you with?"
