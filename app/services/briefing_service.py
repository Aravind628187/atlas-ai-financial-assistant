"""
Builds the personalized morning brief: pulls real quotes + news for the
user's watchlist/preferred sectors, then asks Gemini to write it up —
grounded in retrieved facts, never invented.
"""
from __future__ import annotations

import json
import logging

from app.ai.gemini_client import gemini
from app.ai.prompts import DAILY_BRIEFING_SYSTEM
from app.models import User, WatchlistItem, Preference
from app.services import market_data, news_service

logger = logging.getLogger("atlas.briefing")


def build_morning_brief(user: User, watchlist: list[WatchlistItem], preferences: list[Preference]) -> str | None:
    symbols = [w.symbol for w in watchlist][:8]
    sectors = [p.value for p in preferences if p.key in ("sector_interest", "sector")]

    quotes = [market_data.get_quote(s) for s in symbols]
    quotes = [q.__dict__ for q in quotes if q]

    news_items: list[dict] = []
    for s in symbols[:3]:
        news_items.extend(market_data.get_recent_news(s, limit=2))
    if sectors:
        news_items.extend(news_service.search_news(" OR ".join(sectors[:2]), limit=3))
    if not symbols and not sectors:
        news_items.extend(news_service.search_market_news(limit=5))

    if not quotes and not news_items:
        return None  # nothing to say — silence beats noise per the brief

    payload = {
        "user_role": user.role,
        "watchlist_quotes": quotes,
        "followed_sectors": sectors,
        "news": news_items[:8],
    }
    prompt = f"Data for today's brief:\n{json.dumps(payload, default=str)[:6000]}"
    try:
        return gemini.generate(prompt, system_instruction=DAILY_BRIEFING_SYSTEM, temperature=0.5)
    except Exception:
        logger.exception("Failed to generate morning brief for user %s", user.id)
        return None
