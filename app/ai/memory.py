"""
Two kinds of memory:

1. Short-term conversational memory — the last N turns, fed back into
   Gemini so replies stay contextual ("track it too" after mentioning a stock).
2. Long-term personalization — durable facts (role, followed sectors,
   preferred briefing time...) silently extracted after each turn and
   upserted into the `Preference` table. This is what makes the assistant
   get better the more the user talks to it, per the brief's requirement.
"""
from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.gemini_client import GeminiUnavailableError, gemini
from app.ai.llm_gateway import SecondaryLLMUnavailableError, llm_gateway
from app.ai.prompts import PERSONALIZATION_EXTRACTOR_SYSTEM
from app.models import Message, Preference, User

logger = logging.getLogger("atlas.memory")

HISTORY_TURNS = 12


def get_recent_history(db: Session, user: User, limit: int = HISTORY_TURNS) -> list[dict[str, str]]:
    rows = (
        db.execute(
            select(Message).where(Message.user_id == user.id).order_by(Message.created_at.desc()).limit(limit)
        )
        .scalars()
        .all()
    )
    rows.reverse()
    return [{"role": m.role, "content": m.content} for m in rows]


def log_message(db: Session, user: User, role: str, content: str, intent: str | None = None, input_kind: str = "text") -> None:
    db.add(Message(user_id=user.id, role=role, content=content, intent=intent, input_kind=input_kind))
    # Commit with the caller. Flushing before provider retrieval would hold a
    # SQLite write lock while provider telemetry uses its own short transaction.


def get_preferences(db: Session, user: User) -> dict[str, str]:
    rows = db.execute(select(Preference).where(Preference.user_id == user.id)).scalars().all()
    return {p.key: p.value for p in rows}


def upsert_preference(db: Session, user: User, key: str, value: str) -> None:
    existing = db.execute(
        select(Preference).where(Preference.user_id == user.id, Preference.key == key)
    ).scalar_one_or_none()
    if existing:
        existing.value = value
    else:
        db.add(Preference(user_id=user.id, key=key, value=value))
    db.flush()


def extract_and_store_personalization(db: Session, user: User, user_message: str) -> None:
    """Best-effort, non-blocking-feeling extraction of durable facts from one turn."""
    durable_markers = ("i prefer", "i'm interested", "i am interested", "i follow", "briefing at", "keep answers", "my role")
    if not any(marker in user_message.lower() for marker in durable_markers):
        return
    try:
        data = llm_gateway.generate_json(user_message, system_instruction=PERSONALIZATION_EXTRACTOR_SYSTEM)
        for fact in data.get("facts", []):
            key, value = fact.get("key"), fact.get("value")
            if key and value:
                upsert_preference(db, user, key[:64], str(value)[:512])
    except (GeminiUnavailableError, SecondaryLLMUnavailableError):
        logger.info("Skipping personalization while Gemini is unavailable")
    except Exception:  # noqa: BLE001 — personalization must never break the chat flow
        logger.exception("Personalization extraction failed; continuing without it.")
