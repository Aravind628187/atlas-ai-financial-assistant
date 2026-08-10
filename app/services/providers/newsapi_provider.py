"""Normalized NewsAPI provider; absent keys are explicitly NOT_CONFIGURED."""
from __future__ import annotations

import datetime as dt
import hashlib
import httpx

from app.config import settings
from app.services.providers.base import DataStatus, FinancialDataResult, NewsItem, unavailable_result

UTC = dt.timezone.utc


def _datetime(value):
    try: return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except (ValueError, TypeError): return None


class NewsAPIProvider:
    name = "newsapi"

    def __init__(self, api_key: str = "", client=None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds)
        self.url = "https://newsapi.org/v2/everything"

    @property
    def configured(self): return bool(self.api_key)

    def _missing(self, symbol="MARKET"):
        status = DataStatus.NOT_CONFIGURED if not self.configured else DataStatus.UNAVAILABLE
        return unavailable_result(self.name, symbol, "news", "API key is not configured" if not self.configured else "no news", status)

    def _search(self, query, symbol, limit):
        if not self.configured: return self._missing(symbol)
        response = self.client.get(self.url, params={"q": query, "sortBy": "publishedAt", "pageSize": min(limit * 3, 50), "language": "en"},
                                   headers={"X-Api-Key": self.api_key})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "ok": raise RuntimeError(payload.get("code") or "provider_error")
        items = []
        for row in payload.get("articles", []):
            title, url = row.get("title"), row.get("url")
            if not title: continue
            items.append(NewsItem(
                hashlib.sha256(f"{title}|{url}".encode()).hexdigest()[:16], title,
                (row.get("source") or {}).get("name"), _datetime(row.get("publishedAt")),
                url, row.get("description") or row.get("content"),
            ))
        return FinancialDataResult(DataStatus.OK if items else DataStatus.UNAVAILABLE, self.name, "news", dt.datetime.now(UTC),
                                   max((x.published_at for x in items if x.published_at), default=None), symbol, items or None, False, True)

    def get_news(self, symbol, limit=5):
        return self._search(symbol.upper(), symbol.upper(), limit)

    def get_market_news(self, limit=6):
        return self._search("stock market OR Wall Street OR Nasdaq OR Federal Reserve", "MARKET", limit)

    def get_quote(self, symbol): return self._missing(symbol)
    def get_profile(self, symbol): return self._missing(symbol)
    def get_fundamentals(self, symbol): return self._missing(symbol)
    def get_history(self, symbol, period): return self._missing(symbol)
    def get_earnings(self, symbol): return self._missing(symbol)
