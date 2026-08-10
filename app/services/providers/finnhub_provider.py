"""Finnhub provider used when MARKET_DATA_PROVIDER=finnhub and a key is configured."""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import fields

import httpx

from app.config import settings
from app.services.providers.base import (
    CompanyProfile, DataStatus, EarningsData, FinancialDataResult, FundamentalsData,
    HistoricalData, NewsItem, QuoteData, has_fundamental_values,
    earliest_future_earnings,
)
from app.services.providers.normalization import multiple, positive_money, ratio


UTC = dt.timezone.utc


class FinnhubProvider:
    name = "finnhub"

    def __init__(self, api_key: str = "", client=None):
        self.api_key = api_key
        self.client = client or httpx
        self.base_url = "https://finnhub.io/api/v1"

    @property
    def configured(self):
        return bool(self.api_key)

    def _missing(self, symbol: str, operation: str, message: str = "no supported data",
                 status: DataStatus | None = None):
        return FinancialDataResult(
            status=status or (DataStatus.NOT_CONFIGURED if not self.configured else DataStatus.UNAVAILABLE),
            source=self.name, source_type=operation, retrieved_at=dt.datetime.now(UTC),
            data_as_of=None, symbol=symbol.upper(), data=None, is_realtime=False,
            is_delayed=False, error="API key is not configured" if not self.configured else message,
        )

    def _get(self, path: str, **params):
        if "_from" in params:
            params["from"] = params.pop("_from")
        params["token"] = self.api_key
        response = self.client.get(f"{self.base_url}/{path}", params=params, timeout=settings.financial_provider_timeout_seconds)
        response.raise_for_status()
        return response.json()

    @staticmethod
    def _failure(exc: Exception) -> DataStatus:
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code == 401:
            return DataStatus.INVALID_CREDENTIALS
        if code == 429:
            return DataStatus.RATE_LIMITED
        if code == 403:
            return DataStatus.NOT_ENTITLED
        return DataStatus.ERROR

    def _safe(self, symbol: str, operation: str, callback):
        if not self.configured:
            return self._missing(symbol, operation)
        try:
            return callback()
        except httpx.HTTPStatusError as exc:
            return self._missing(symbol, operation, self._failure(exc).value, self._failure(exc))

    def _result(self, symbol, data, *, as_of=None, source_type="market_data"):
        return FinancialDataResult(
            status=DataStatus.OK if data else DataStatus.UNAVAILABLE, source=self.name,
            source_type=source_type, retrieved_at=dt.datetime.now(UTC), data_as_of=as_of,
            # Finnhub entitlements vary by account; without explicit entitlement metadata
            # Atlas must conservatively describe the quote as delayed/latest available.
            symbol=symbol.upper(), data=data, is_realtime=False, is_delayed=True,
        )

    def get_quote(self, symbol: str) -> FinancialDataResult:
        def fetch():
            raw = self._get("quote", symbol=symbol.upper())
            price = raw.get("c") or None
            previous = raw.get("pc") or None
            as_of = dt.datetime.fromtimestamp(raw["t"], tz=UTC) if raw.get("t") else None
            data = QuoteData(symbol.upper(), price, previous, raw.get("d"), raw.get("dp"), None) if price else None
            result = self._result(symbol, data, as_of=as_of)
            result.timestamp_kind = "quote" if as_of else None
            return result
        return self._safe(symbol, "quote", fetch)

    def get_profile(self, symbol: str) -> FinancialDataResult:
        def fetch():
            raw = self._get("stock/profile2", symbol=symbol.upper())
            data = CompanyProfile(symbol.upper(), raw.get("name"), None,
                                  raw.get("finnhubIndustry"), None, raw.get("weburl")) if raw.get("name") else None
            return self._result(symbol, data)
        return self._safe(symbol, "profile", fetch)

    def get_fundamentals(self, symbol: str) -> FinancialDataResult:
        def fetch():
            raw = (self._get("stock/metric", symbol=symbol.upper(), metric="all") or {}).get("metric", {})
            indicated = ratio(raw.get("dividendYieldIndicatedAnnual"), unit="percent", minimum=0, maximum=1)
            data = FundamentalsData(
            symbol.upper(),
            market_cap=float(raw["marketCapitalization"]) * 1_000_000 if raw.get("marketCapitalization") is not None else None,
            trailing_pe=multiple(raw.get("peBasicExclExtraTTM")), forward_pe=multiple(raw.get("peExclExtraAnnual")),
            profit_margin=ratio(raw.get("netProfitMarginTTM"), unit="percent"),
            revenue_growth=ratio(raw.get("revenueGrowthTTMYoy"), unit="percent"),
            fifty_two_week_high=positive_money(raw.get("52WeekHigh")),
            fifty_two_week_low=positive_money(raw.get("52WeekLow")),
            dividend_yield=indicated,
            revenue_period="ttm", margin_period="ttm",
            unit_notes={"ratios": "Finnhub percentage fields normalized from percent to fraction"},
            dividend_yield_indicated=indicated, revenue_growth_period="ttm_yoy",
            metric_definitions={"trailing_pe": "trailing", "profit_margin": "ttm_net",
                                "revenue_growth": "ttm_yoy", "dividend_yield": "indicated"},
            )
            available = has_fundamental_values(data)
            return self._result(symbol, data if available else None)
        return self._safe(symbol, "fundamentals", fetch)

    def get_news(self, symbol: str, limit: int = 5) -> FinancialDataResult:
        def fetch():
            today = dt.date.today()
            raw = self._get("company-news", symbol=symbol.upper(), _from=str(today - dt.timedelta(days=7)), to=str(today))
            items = [NewsItem(
            news_id=str(row.get("id") or hashlib.sha256(str(row).encode()).hexdigest()[:16]),
            headline=row.get("headline", ""), publisher=row.get("source"),
            published_at=dt.datetime.fromtimestamp(row["datetime"], tz=UTC) if row.get("datetime") else None,
            url=row.get("url"), summary=row.get("summary"),
            ) for row in raw[:limit] if row.get("headline")]
            return self._result(symbol, items, as_of=max((x.published_at for x in items if x.published_at), default=None), source_type="news")
        return self._safe(symbol, "news", fetch)

    def get_earnings(self, symbol: str) -> FinancialDataResult:
        def fetch():
            today = dt.date.today()
            raw = self._get("calendar/earnings", symbol=symbol.upper(), _from=str(today), to=str(today + dt.timedelta(days=180)))
            rows = raw.get("earningsCalendar", []) if isinstance(raw, dict) else []
            value, row = earliest_future_earnings(rows, ("date", "earningsDate"))
            if not value or not row:
                return self._result(symbol, None)
            confirmed = row.get("confirmed") is True or row.get("isConfirmed") is True or str(row.get("status", "")).lower() == "confirmed"
            return self._result(symbol, EarningsData(
                symbol.upper(), value, row.get("quarter"),
                "confirmed" if confirmed else "estimated", self.name,
            ))
        return self._safe(symbol, "earnings", fetch)

    def get_history(self, symbol: str, period: str) -> FinancialDataResult:
        # Finnhub candle access depends on subscription tier; fail closed instead of pretending.
        return self._result(symbol, None)
