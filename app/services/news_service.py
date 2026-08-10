"""
News retrieval. Defaults to Google News RSS (free, no key) so the project
runs out of the box; automatically upgrades to NewsAPI.org if NEWS_API_KEY
is set in .env for higher-quality, deduped results.
"""
from __future__ import annotations

import logging
from urllib.parse import quote_plus

import feedparser
import httpx

from app.config import settings

logger = logging.getLogger("atlas.news")


def _google_news_rss(query: str, limit: int) -> list[dict]:
    try:
        url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
        response = httpx.get(url, timeout=10, follow_redirects=True)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        if getattr(feed, "bozo", False) and not feed.entries:
            return []
        out = []
        for entry in feed.entries[:limit]:
            if not entry.get("title"):
                continue
            out.append(
                {
                    "title": entry.get("title"),
                    "publisher": entry.get("source", {}).get("title") if entry.get("source") else None,
                    "link": entry.get("link"),
                    "published": entry.get("published"),
                }
            )
        return out
    except Exception:
        logger.exception("Google News RSS request failed")
        return []


def _newsapi(query: str, limit: int) -> list[dict]:
    try:
        resp = httpx.get(
            "https://newsapi.org/v2/everything",
            params={"q": query, "sortBy": "publishedAt", "pageSize": limit, "language": "en"},
            headers={"X-Api-Key": settings.news_api_key},
            timeout=10,
        )
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
        return [
            {
                "title": a.get("title"),
                "publisher": (a.get("source") or {}).get("name"),
                "link": a.get("url"),
                "published": a.get("publishedAt"),
            }
            for a in articles
        ]
    except Exception:
        logger.exception("NewsAPI request failed, falling back to RSS")
        return _google_news_rss(query, limit)


def search_news(query: str, limit: int = 6) -> list[dict]:
    if settings.news_api_key:
        return _newsapi(query, limit)
    return _google_news_rss(query, limit)


def search_market_news(limit: int = 6) -> list[dict]:
    return search_news("stock market OR Wall Street OR Nasdaq OR Fed", limit=limit)
