"""Google News RSS fallback normalized to the common news contract."""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
from urllib.parse import quote_plus

import feedparser
import httpx

from app.config import settings
from app.services.entity_resolution import KNOWN_COMPANIES
from app.services.providers.base import DataStatus, FinancialDataResult, NewsItem, unavailable_result

UTC = dt.timezone.utc


class RSSNewsProvider:
    name = "rss"
    configured = True

    def __init__(self, client=None):
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds, follow_redirects=True)

    def get_news(self, symbol: str, limit: int = 5) -> FinancialDataResult:
        company = next((name for name, ticker in KNOWN_COMPANIES.items() if ticker == symbol.upper()), symbol.upper())
        query = quote_plus(f'"{company}" OR {symbol.upper()}')
        response = self.client.get(f"https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en")
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        items: list[NewsItem] = []
        for entry in feed.entries[: min(limit * 3, 30)]:
            headline = entry.get("title")
            if not headline:
                continue
            raw_date = entry.get("published")
            parsed = email.utils.parsedate_to_datetime(raw_date).astimezone(UTC) if raw_date else None
            source = entry.get("source") or {}
            url = entry.get("link")
            items.append(NewsItem(
                hashlib.sha256(f"{headline}|{url}".encode()).hexdigest()[:16], headline,
                source.get("title") if isinstance(source, dict) else None, parsed, url,
                entry.get("summary"),
            ))
        if not items:
            return unavailable_result(self.name, symbol, "news", "no news")
        return FinancialDataResult(
            DataStatus.OK, self.name, "news", dt.datetime.now(UTC),
            max((item.published_at for item in items if item.published_at), default=None),
            symbol.upper(), items, False, True,
        )

