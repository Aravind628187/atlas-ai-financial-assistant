"""Twelve Data quote and daily time-series provider."""
from __future__ import annotations

import datetime as dt
import httpx

from app.config import settings
from app.services.providers.base import DataStatus, FinancialDataResult, HistoricalData, HistoricalPoint, QuoteData, unavailable_result
from app.services.providers.normalization import epoch_datetime, number, positive_money

UTC = dt.timezone.utc


class TwelveDataProvider:
    name = "twelve_data"

    def __init__(self, api_key: str = "", client=None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds)
        self.base_url = "https://api.twelvedata.com"

    @property
    def configured(self): return bool(self.api_key)

    def _missing(self, symbol, operation):
        status = DataStatus.NOT_CONFIGURED if not self.configured else DataStatus.UNAVAILABLE
        return unavailable_result(self.name, symbol, operation, "API key is not configured" if not self.configured else "no supported data", status)

    def _get(self, path, **params):
        response = self.client.get(f"{self.base_url}/{path}", params={**params, "apikey": self.api_key})
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") == "error" or payload.get("code") in {401, 403, 429}:
            raise RuntimeError(payload.get("message") or "provider_error")
        return payload

    def get_quote(self, symbol):
        if not self.configured: return self._missing(symbol, "quote")
        row = self._get("quote", symbol=symbol.upper())
        price = positive_money(row.get("close"))
        if price is None: return self._missing(symbol, "quote")
        data = QuoteData(symbol.upper(), price, positive_money(row.get("previous_close")), number(row.get("change")),
                         number(row.get("percent_change")), row.get("currency"), row.get("name"),
                         "extended" if row.get("is_extended_hours") else None)
        result = FinancialDataResult(DataStatus.OK, self.name, "quote", dt.datetime.now(UTC), epoch_datetime(row.get("timestamp")),
                                     symbol.upper(), data, False, True)
        result.timestamp_kind = "quote" if result.data_as_of else None
        result.exchange_timezone = row.get("exchange_timezone") or row.get("timezone")
        return result

    def get_history(self, symbol, period):
        if not self.configured: return self._missing(symbol, "history")
        sizes = {"1mo": 35, "3mo": 100, "6mo": 140, "1y": 260}
        payload = self._get("time_series", symbol=symbol.upper(), interval="1day", outputsize=sizes.get(period, 35), order="ASC")
        points = []
        for row in payload.get("values", []):
            # Twelve Data daily values use a session date label, not a trade
            # timestamp. Midnight UTC is retained only for ordering/backward
            # compatibility and is never rendered as an intraday quote time.
            try: stamp = dt.datetime.combine(dt.date.fromisoformat(row["datetime"][:10]), dt.time(), tzinfo=UTC)
            except (KeyError, ValueError): continue
            points.append(HistoricalPoint(stamp, number(row.get("open")), number(row.get("high")), number(row.get("low")),
                                          number(row.get("close")), int(float(row["volume"])) if number(row.get("volume")) is not None else None))
        data = HistoricalData(symbol.upper(), points)
        if not points:
            return self._missing(symbol, "history")
        result = FinancialDataResult(DataStatus.OK, self.name, "history", dt.datetime.now(UTC), None,
                                     symbol.upper(), data, False, True)
        result.timestamp_kind = "daily_bar_date"
        result.data_date = points[-1].timestamp.date()
        result.interval = "1day"
        result.exchange_timezone = (payload.get("meta") or {}).get("exchange_timezone") or (payload.get("meta") or {}).get("timezone")
        return result

    def get_profile(self, symbol): return self._missing(symbol, "profile")
    def get_fundamentals(self, symbol): return self._missing(symbol, "fundamentals")
    def get_earnings(self, symbol): return self._missing(symbol, "earnings")
    def get_news(self, symbol, limit=5): return self._missing(symbol, "news")
