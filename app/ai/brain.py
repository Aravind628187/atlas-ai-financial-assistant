"""
The assistant's "brain": one entry point, `handle_text_turn`, that every
channel (text, transcribed voice, post-document follow-up) calls through.

Flow for a normal turn:
  1. Onboarding not finished? -> hand off to onboarding.py
  2. Otherwise classify intent + entities with Gemini
  3. Handle manual briefing requests
  4. Fetch REAL data from the relevant service
  5. Ask Gemini to write the reply
  6. Log the turn + personalization
"""

from __future__ import annotations

import datetime as dt
import json
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai import onboarding
from app.ai.gemini_client import gemini
from app.ai.intent_router import route
from app.ai.memory import (
    extract_and_store_personalization,
    get_preferences,
    get_recent_history,
    log_message,
)
from app.ai.prompts import ASSISTANT_PERSONA

from app.models import (
    Alert,
    BriefingLog,
    Document,
    OnboardingStage,
    Preference,
    User,
    WatchlistItem,
)

from app.services import (
    google_integrations,
    market_data,
    news_service,
)

from app.services.briefing_service import build_morning_brief
from app.services.document_service import answer_question_about_document


logger = logging.getLogger("atlas.brain")


# ============================================================
# Manual briefing detection
# ============================================================

def _is_manual_briefing_request(text: str) -> bool:
    """
    Detect requests where the user wants the briefing immediately.

    This intentionally does not handle requests such as
    'change my briefing time', which belong to schedule_briefing.
    """

    text = text.strip().lower()

    direct_requests = {
        "give me my morning briefing",
        "give me my briefing",
        "morning briefing",
        "my morning briefing",
        "today's briefing",
        "todays briefing",
        "daily briefing",
        "show my briefing",
        "send my briefing",
    }

    if text in direct_requests:
        return True

    has_briefing = (
        "briefing" in text
        or "morning brief" in text
    )

    wants_now = any(
        phrase in text
        for phrase in (
            "give me",
            "show me",
            "send me",
            "tell me",
            "what's my",
            "whats my",
            "today",
            "right now",
        )
    )

    return has_briefing and wants_now


# ============================================================
# Grounding
# ============================================================

def _grounding_for_intent(
    db: Session,
    user: User,
    routed,
) -> dict:

    """
    Fetch real facts based on the classified intent.
    """

    data: dict = {}

    symbols = routed.symbols


    # --------------------------------------------------------
    # Market data
    # --------------------------------------------------------

    if routed.intent == "market_data" and symbols:

        quotes = []

        for symbol in symbols:
            quote = market_data.get_quote(symbol)

            if quote:
                quotes.append(quote.__dict__)

        data["quotes"] = quotes


    # --------------------------------------------------------
    # Company research
    # --------------------------------------------------------

    elif routed.intent == "company_research":

        if symbols:

            data["profiles"] = (
                market_data.compare_symbols(
                    symbols
                )
            )

        elif routed.companies:

            data["news"] = (
                news_service.search_news(
                    " OR ".join(
                        routed.companies
                    ),
                    limit=6,
                )
            )


    # --------------------------------------------------------
    # News
    # --------------------------------------------------------

    elif routed.intent == "news":

        query = (
            " OR ".join(
                symbols
                + routed.companies
            )
            or "stock market"
        )

        data["news"] = (
            news_service.search_news(
                query,
                limit=8,
            )
        )


    # --------------------------------------------------------
    # Earnings
    # --------------------------------------------------------

    elif routed.intent == "earnings":

        for symbol in symbols:

            data.setdefault(
                "earnings",
                [],
            ).append(
                market_data.get_earnings_calendar(
                    symbol
                )
            )

            data.setdefault(
                "news",
                [],
            ).extend(
                market_data.get_recent_news(
                    symbol,
                    limit=3,
                )
            )


    # --------------------------------------------------------
    # Add to watchlist
    # --------------------------------------------------------

    elif (
        routed.intent == "watchlist_add"
        and symbols
    ):

        added = []

        for symbol in symbols:

            exists = (
                db.execute(
                    select(
                        WatchlistItem
                    ).where(
                        WatchlistItem.user_id
                        == user.id,
                        WatchlistItem.symbol
                        == symbol,
                    )
                )
                .scalar_one_or_none()
            )

            if not exists:

                db.add(
                    WatchlistItem(
                        user_id=user.id,
                        symbol=symbol,
                    )
                )

                added.append(symbol)

        db.flush()

        data["added_to_watchlist"] = added


    # --------------------------------------------------------
    # View watchlist
    # --------------------------------------------------------

    elif routed.intent == "watchlist_view":

        items = (
            db.execute(
                select(
                    WatchlistItem
                ).where(
                    WatchlistItem.user_id
                    == user.id
                )
            )
            .scalars()
            .all()
        )

        data["watchlist"] = [
            item.symbol
            for item in items
        ]

        if data["watchlist"]:

            quotes = []

            for symbol in data["watchlist"]:

                quote = (
                    market_data.get_quote(
                        symbol
                    )
                )

                if quote:
                    quotes.append(
                        quote.__dict__
                    )

            data["quotes"] = quotes


    # --------------------------------------------------------
    # Create alert
    # --------------------------------------------------------

    elif (
        routed.intent == "alert_create"
        and symbols
    ):

        created = []

        for symbol in symbols:

            # Avoid creating exact duplicate active alerts
            existing = (
                db.execute(
                    select(Alert).where(
                        Alert.user_id
                        == user.id,
                        Alert.symbol
                        == symbol,
                        Alert.kind
                        == "pct_move",
                        Alert.active.is_(True),
                    )
                )
                .scalar_one_or_none()
            )

            if existing:
                continue

            db.add(
                Alert(
                    user_id=user.id,
                    symbol=symbol,
                    kind="pct_move",
                    threshold_pct=5.0,
                    active=True,
                )
            )

            created.append(symbol)

        db.flush()

        data["alerts_created"] = created


    # --------------------------------------------------------
    # Google integration
    # --------------------------------------------------------

    elif routed.intent == "integration_connect":

        provider = next(
            (
                company.lower()
                for company
                in routed.companies
                if company.lower()
                in {
                    "gmail",
                    "calendar",
                    "drive",
                    "sheets",
                }
            ),
            "gmail",
        )

        if google_integrations.is_configured():

            data["oauth_url"] = (
                google_integrations.build_consent_url(
                    provider,
                    user.telegram_id,
                )
            )

            data["provider"] = provider

        else:

            data[
                "integration_not_configured"
            ] = provider


    # --------------------------------------------------------
    # Document question answering
    # --------------------------------------------------------

    elif routed.intent == "document_qa":

        if user.last_document_id:

            document = db.get(
                Document,
                user.last_document_id,
            )

            if (
                document
                and document.extracted_text
            ):

                data["document_answer"] = (
                    answer_question_about_document(
                        document.extracted_text,
                        routed.clarifying_question
                        or "",
                    )
                )

    return data


# ============================================================
# Normal Gemini reply
# ============================================================

def _build_reply(
    user: User,
    user_message: str,
    history: list[dict],
    grounding: dict,
    preferences: dict,
) -> str:

    context_block = {
        "user_role": user.role,
        "known_preferences": preferences,
        "retrieved_data": grounding,
    }

    prompt = (
        "Context "
        "(facts you may use — do not invent "
        "numbers beyond these):\n"
        f"{json.dumps(context_block, default=str)[:6000]}"
        "\n\n"
        f"User's message: {user_message}"
    )

    return gemini.generate(
        prompt,
        system_instruction=ASSISTANT_PERSONA,
        history=history,
        temperature=0.6,
    )


# ============================================================
# Manual briefing
# ============================================================

def _handle_manual_briefing(
    db: Session,
    user: User,
) -> str:

    """
    Generate a briefing immediately and save it
    to briefing_logs so Mission Control can show it.
    """

    watchlist = (
        db.execute(
            select(
                WatchlistItem
            ).where(
                WatchlistItem.user_id
                == user.id
            )
        )
        .scalars()
        .all()
    )

    preferences = (
        db.execute(
            select(
                Preference
            ).where(
                Preference.user_id
                == user.id
            )
        )
        .scalars()
        .all()
    )

    briefing = build_morning_brief(
        user,
        watchlist,
        preferences,
    )

    if not briefing:

        return (
            "I don't have enough fresh market data "
            "to build a useful briefing right now. "
            "Try again shortly."
        )

    # Important:
    # Save manual briefing in briefing_logs.
    db.add(
        BriefingLog(
            user_id=user.id,
            kind="morning_brief",
            content=briefing,
        )
    )

    db.flush()

    return briefing


# ============================================================
# Main text entry point
# ============================================================

def handle_text_turn(
    db: Session,
    user: User,
    text: str,
    input_kind: str = "text",
) -> str:

    # --------------------------------------------------------
    # Update user activity
    # --------------------------------------------------------

    user.last_active_at = (
        dt.datetime.utcnow()
    )


    # --------------------------------------------------------
    # Log incoming user message
    # --------------------------------------------------------

    log_message(
        db,
        user,
        "user",
        text,
        input_kind=input_kind,
    )


    # --------------------------------------------------------
    # Onboarding
    # --------------------------------------------------------

    if (
        user.onboarding_stage
        != OnboardingStage.DONE.value
    ):

        reply = (
            onboarding.handle_onboarding_turn(
                db,
                user,
                text,
            )
        )

        log_message(
            db,
            user,
            "assistant",
            reply,
            intent="onboarding",
        )

        return reply


    # --------------------------------------------------------
    # Intent classification
    # --------------------------------------------------------

    routed = route(text)


    # --------------------------------------------------------
    # Manual morning briefing
    # --------------------------------------------------------

    if (
        routed.intent
        != "schedule_briefing"
        and _is_manual_briefing_request(
            text
        )
    ):

        try:

            reply = (
                _handle_manual_briefing(
                    db,
                    user,
                )
            )

        except Exception:

            logger.exception(
                "Manual briefing failed "
                "for user %s",
                user.id,
            )

            reply = (
                "I couldn't generate your "
                "briefing right now. "
                "Please try again shortly."
            )


        log_message(
            db,
            user,
            "assistant",
            reply,
            intent="briefing",
        )

        extract_and_store_personalization(
            db,
            user,
            text,
        )

        return reply


    # --------------------------------------------------------
    # Clarification
    # --------------------------------------------------------

    if (
        routed.intent == "clarify"
        and routed.clarifying_question
    ):

        reply = (
            routed.clarifying_question
        )

        log_message(
            db,
            user,
            "assistant",
            reply,
            intent="clarify",
        )

        return reply


    # --------------------------------------------------------
    # Retrieve grounded real-world data
    # --------------------------------------------------------

    grounding = (
        _grounding_for_intent(
            db,
            user,
            routed,
        )
    )


    # --------------------------------------------------------
    # History / preferences
    # --------------------------------------------------------

    history = (
        get_recent_history(
            db,
            user,
        )
    )

    preferences = (
        get_preferences(
            db,
            user,
        )
    )


    # --------------------------------------------------------
    # Generate assistant reply
    # --------------------------------------------------------

    reply = _build_reply(
        user,
        text,
        history[:-1]
        if history
        else [],
        grounding,
        preferences,
    )


    # --------------------------------------------------------
    # Log assistant response
    # --------------------------------------------------------

    log_message(
        db,
        user,
        "assistant",
        reply,
        intent=routed.intent,
    )


    # --------------------------------------------------------
    # Update personalization
    # --------------------------------------------------------

    #extract_and_store_personalization(
       # db,
        #user,
       # text,
    #)


    return reply