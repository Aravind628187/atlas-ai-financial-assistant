"""Optional Massive (formerly Polygon) stock snapshot provider."""
from __future__ import annotations

import datetime as dt
import httpx

from app.config import settings
from app.services.providers.base import DataStatus, FinancialDataResult, QuoteData, unavailable_result
from app.services.providers.normalization import epoch_datetime, number, positive_money

UTC = dt.timezone.utc


class MassiveProvider:
    name = "massive"

    def __init__(self, api_key: str = "", client=None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds)
        self.base_url = "https://api.massive.com"

    @property
    def configured(self): return bool(self.api_key)

    def _missing(self, symbol, operation="quote"):
        status = DataStatus.NOT_CONFIGURED if not self.configured else DataStatus.UNAVAILABLE
        return unavailable_result(self.name, symbol, operation, "API key is not configured" if not self.configured else "no supported data", status)

    def get_quote(self, symbol):
        if not self.configured: return self._missing(symbol)
        response = self.client.get(f"{self.base_url}/v2/snapshot/locale/us/markets/stocks/tickers/{symbol.upper()}",
                                   params={"apiKey": self.api_key})
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            code = getattr(exc.response, "status_code", None)
            if code == 403:
                return unavailable_result(self.name, symbol, "quote", "not_entitled", DataStatus.NOT_ENTITLED)
            if code == 401:
                return unavailable_result(self.name, symbol, "quote", "invalid_credentials", DataStatus.INVALID_CREDENTIALS)
            if code == 429:
                return unavailable_result(self.name, symbol, "quote", "rate_limited", DataStatus.RATE_LIMITED)
            raise
        row = response.json().get("ticker", {})
        price = positive_money((row.get("lastTrade") or {}).get("p") or (row.get("day") or {}).get("c"))
        previous = positive_money((row.get("prevDay") or {}).get("c"))
        if price is None: return self._missing(symbol)
        data = QuoteData(symbol.upper(), price, previous, number(row.get("todaysChange")), number(row.get("todaysChangePerc")))
        return FinancialDataResult(DataStatus.OK, self.name, "quote", dt.datetime.now(UTC),
                                   epoch_datetime(row.get("updated"), divisor=1e9), symbol.upper(), data, False, True)

    def get_profile(self, symbol): return self._missing(symbol, "profile")
    def get_fundamentals(self, symbol): return self._missing(symbol, "fundamentals")
    def get_history(self, symbol, period): return self._missing(symbol, "history")
    def get_earnings(self, symbol): return self._missing(symbol, "earnings")
    def get_news(self, symbol, limit=5): return self._missing(symbol, "news")
