"""Alpha Vantage overview and daily historical data provider."""
from __future__ import annotations

import datetime as dt
import csv
import io
from dataclasses import fields
import httpx

from app.config import settings
from app.services.providers.base import CompanyProfile, DataStatus, EarningsData, FinancialDataResult, FundamentalsData, HistoricalData, HistoricalPoint, earliest_future_earnings, has_fundamental_values, unavailable_result
from app.services.providers.normalization import multiple, number, positive_money, ratio

UTC = dt.timezone.utc


class AlphaVantageProvider:
    name = "alpha_vantage"

    def __init__(self, api_key: str = "", client=None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds)
        self.url = "https://www.alphavantage.co/query"

    @property
    def configured(self): return bool(self.api_key)

    def _missing(self, symbol, operation):
        status = DataStatus.NOT_CONFIGURED if not self.configured else DataStatus.UNAVAILABLE
        return unavailable_result(self.name, symbol, operation, "API key is not configured" if not self.configured else "no supported data", status)

    def _get(self, function, **params):
        response = self.client.get(self.url, params={"function": function, **params, "apikey": self.api_key})
        response.raise_for_status()
        payload = response.json()
        if any(k in payload for k in ("Error Message", "Note", "Information")):
            raise RuntimeError(payload.get("Error Message") or payload.get("Note") or payload.get("Information"))
        return payload

    def get_profile(self, symbol):
        if not self.configured: return self._missing(symbol, "profile")
        row = self._get("OVERVIEW", symbol=symbol.upper())
        if not row.get("Symbol"): return self._missing(symbol, "profile")
        data = CompanyProfile(symbol.upper(), row.get("Name"), row.get("Sector"), row.get("Industry"), row.get("Description"), None)
        return FinancialDataResult(DataStatus.OK, self.name, "profile", dt.datetime.now(UTC), None, symbol.upper(), data, False, True)

    def get_fundamentals(self, symbol):
        if not self.configured: return self._missing(symbol, "fundamentals")
        row = self._get("OVERVIEW", symbol=symbol.upper())
        data = FundamentalsData(
            symbol.upper(), market_cap=positive_money(row.get("MarketCapitalization")),
            trailing_pe=multiple(row.get("PERatio")), forward_pe=multiple(row.get("ForwardPE")),
            revenue=positive_money(row.get("RevenueTTM")),
            profit_margin=ratio(row.get("ProfitMargin"), unit="fraction"),
            revenue_growth=ratio(row.get("QuarterlyRevenueGrowthYOY"), unit="fraction"),
            fifty_two_week_high=positive_money(row.get("52WeekHigh")),
            fifty_two_week_low=positive_money(row.get("52WeekLow")),
            dividend_yield=ratio(row.get("DividendYield"), unit="fraction", minimum=0, maximum=1),
            revenue_period="ttm", margin_period="ttm",
            unit_notes={"ratios": "Alpha Vantage OVERVIEW ratio fields are decimal fractions"},
            revenue_growth_period="quarterly_yoy", currency=row.get("Currency"),
            metric_definitions={"trailing_pe": "trailing", "forward_pe": "forward", "revenue": "ttm",
                                "profit_margin": "ttm_net", "revenue_growth": "quarterly_yoy",
                                "dividend_yield": "unknown", "market_cap": "current"},
        )
        available = has_fundamental_values(data)
        return FinancialDataResult(DataStatus.OK, self.name, "fundamentals", dt.datetime.now(UTC), None, symbol.upper(), data, False, True) if available else self._missing(symbol, "fundamentals")

    def get_history(self, symbol, period):
        if not self.configured: return self._missing(symbol, "history")
        payload = self._get("TIME_SERIES_DAILY", symbol=symbol.upper(), outputsize="compact")
        rows = payload.get("Time Series (Daily)", {})
        limits = {"1mo": 23, "3mo": 66, "6mo": 100, "1y": 100}
        points = []
        for date_value, row in sorted(rows.items())[-limits.get(period, 23):]:
            try: stamp = dt.datetime.fromisoformat(date_value).replace(tzinfo=UTC)
            except ValueError: continue
            points.append(HistoricalPoint(stamp, number(row.get("1. open")), number(row.get("2. high")),
                                          number(row.get("3. low")), number(row.get("4. close")),
                                          int(float(row["5. volume"])) if number(row.get("5. volume")) is not None else None))
        data = HistoricalData(symbol.upper(), points)
        return FinancialDataResult(DataStatus.OK, self.name, "history", dt.datetime.now(UTC), points[-1].timestamp if points else None,
                                   symbol.upper(), data, False, True) if points else self._missing(symbol, "history")

    def get_quote(self, symbol): return self._missing(symbol, "quote")
    def get_earnings(self, symbol):
        if not self.configured: return self._missing(symbol, "earnings")
        response = self.client.get(self.url, params={
            "function": "EARNINGS_CALENDAR", "symbol": symbol.upper(),
            "horizon": "6month", "apikey": self.api_key,
        })
        response.raise_for_status()
        content = response.content.decode("utf-8-sig") if isinstance(response.content, bytes) else str(response.text)
        rows = list(csv.DictReader(io.StringIO(content)))
        when, row = earliest_future_earnings(rows, ("reportDate", "date", "earningsDate"))
        if not when or not row: return self._missing(symbol, "earnings")
        data = EarningsData(symbol.upper(), when, row.get("fiscalDateEnding"), "estimated", self.name)
        return FinancialDataResult(DataStatus.OK, self.name, "earnings", dt.datetime.now(UTC), None,
                                   symbol.upper(), data, False, True)
    def get_news(self, symbol, limit=5): return self._missing(symbol, "news")
