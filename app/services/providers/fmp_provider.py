"""Financial Modeling Prep provider using current stable REST endpoints."""
from __future__ import annotations

import datetime as dt
from dataclasses import fields

import httpx

from app.config import settings
from app.services.providers.base import (
    CompanyProfile, DataStatus, EarningsData, FinancialDataResult, FundamentalsData,
    HistoricalData, HistoricalPoint, QuoteData, has_fundamental_values, unavailable_result,
    earliest_future_earnings,
)
from app.services.providers.normalization import multiple, number, positive_money, ratio


UTC = dt.timezone.utc


class FMPProvider:
    name = "fmp"

    def __init__(self, api_key: str = "", client=None):
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds)
        self.base_url = "https://financialmodelingprep.com/stable"

    @property
    def configured(self):
        return bool(self.api_key)

    def _get(self, path: str, **params):
        if not self.configured:
            raise RuntimeError("not_configured")
        if "_from" in params:
            params["from"] = params.pop("_from")
        response = self.client.get(f"{self.base_url}/{path}", params={**params, "apikey": self.api_key})
        response.raise_for_status()
        return response.json()

    def _missing(self, symbol, operation):
        status = DataStatus.NOT_CONFIGURED if not self.configured else DataStatus.UNAVAILABLE
        return unavailable_result(self.name, symbol, operation, "API key is not configured" if not self.configured else "no supported data", status)

    def _result(self, symbol, operation, data, as_of=None):
        return FinancialDataResult(DataStatus.OK, self.name, operation, dt.datetime.now(UTC), as_of,
                                   symbol.upper(), data, False, True)

    def get_quote(self, symbol: str):
        if not self.configured:
            return self._missing(symbol, "quote")
        rows = self._get("quote", symbol=symbol.upper()) or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        price = positive_money(row.get("price"))
        if price is None:
            return self._missing(symbol, "quote")
        stamp = row.get("timestamp")
        as_of = dt.datetime.fromtimestamp(float(stamp), UTC) if stamp else None
        data = QuoteData(symbol.upper(), price, positive_money(row.get("previousClose")),
                         number(row.get("change")), number(row.get("changePercentage") or row.get("changesPercentage")),
                         row.get("currency"), row.get("name"))
        return self._result(symbol, "quote", data, as_of)

    def get_profile(self, symbol: str):
        if not self.configured:
            return self._missing(symbol, "profile")
        rows = self._get("profile", symbol=symbol.upper()) or []
        row = rows[0] if isinstance(rows, list) and rows else {}
        data = CompanyProfile(symbol.upper(), row.get("companyName"), row.get("sector"), row.get("industry"),
                              row.get("description"), row.get("website")) if row else None
        return self._result(symbol, "profile", data) if data else self._missing(symbol, "profile")

    def get_fundamentals(self, symbol: str):
        if not self.configured:
            return self._missing(symbol, "fundamentals")
        profile_rows = self._get("profile", symbol=symbol.upper()) or []
        ratio_rows = self._get("ratios-ttm", symbol=symbol.upper()) or []
        income_rows = self._get("income-statement-ttm", symbol=symbol.upper(), limit=1) or []
        profile = profile_rows[0] if isinstance(profile_rows, list) and profile_rows else {}
        ratios = ratio_rows[0] if isinstance(ratio_rows, list) and ratio_rows else {}
        income = income_rows[0] if isinstance(income_rows, list) and income_rows else {}
        revenue = positive_money(income.get("revenue"))
        net_income = number(income.get("netIncome"))
        margin = net_income / revenue if revenue and net_income is not None else ratio(
            ratios.get("netProfitMarginTTM"), unit="fraction"
        )
        data = FundamentalsData(
            symbol.upper(), market_cap=positive_money(profile.get("marketCap")),
            trailing_pe=multiple(ratios.get("priceToEarningsRatioTTM") or ratios.get("peRatioTTM")),
            forward_pe=None, revenue=revenue, profit_margin=margin,
            revenue_growth=None, fifty_two_week_high=positive_money(profile.get("range", "").split("-")[-1] if profile.get("range") else None),
            fifty_two_week_low=positive_money(profile.get("range", "").split("-")[0] if profile.get("range") else None),
            dividend_yield=ratio(ratios.get("dividendYieldTTM"), unit="fraction", minimum=0, maximum=1),
            revenue_period="ttm", margin_period="ttm",
            unit_notes={"ratios": "FMP ratios are normalized fractions; margin may be derived from same-period statements"},
            dividend_yield_ttm=ratio(ratios.get("dividendYieldTTM"), unit="fraction", minimum=0, maximum=1),
            currency=profile.get("currency"),
            metric_definitions={"trailing_pe": "trailing", "revenue": "ttm", "profit_margin": "ttm_net",
                                "dividend_yield": "ttm", "market_cap": "current"},
        )
        available = has_fundamental_values(data)
        return self._result(symbol, "fundamentals", data) if available else self._missing(symbol, "fundamentals")

    def get_history(self, symbol: str, period: str):
        if not self.configured:
            return self._missing(symbol, "history")
        days = {"1mo": 40, "3mo": 120, "6mo": 220, "1y": 400}.get(period, 40)
        today = dt.date.today()
        rows = self._get("historical-price-eod/full", symbol=symbol.upper(),
                         _from=str(today - dt.timedelta(days=days)), to=str(today)) or []
        if isinstance(rows, dict):
            rows = rows.get("historical", [])
        points = []
        for row in reversed(rows):
            try:
                stamp = dt.datetime.fromisoformat(row["date"]).replace(tzinfo=UTC)
            except (KeyError, ValueError):
                continue
            points.append(HistoricalPoint(stamp, number(row.get("open")), number(row.get("high")),
                                          number(row.get("low")), number(row.get("close")),
                                          int(row["volume"]) if number(row.get("volume")) is not None else None))
        data = HistoricalData(symbol.upper(), points)
        return self._result(symbol, "history", data, points[-1].timestamp if points else None) if points else self._missing(symbol, "history")

    def get_earnings(self, symbol: str):
        if not self.configured:
            return self._missing(symbol, "earnings")
        rows = self._get("earnings-calendar", symbol=symbol.upper()) or []
        when, row = earliest_future_earnings(rows if isinstance(rows, list) else [], ("date", "earningsDate"))
        if not when or not row:
            return self._missing(symbol, "earnings")
        confirmed = row.get("confirmed") is True or row.get("isConfirmed") is True or str(row.get("status", "")).lower() == "confirmed"
        event = EarningsData(symbol.upper(), when, row.get("fiscalDateEnding"),
                             "confirmed" if confirmed else "estimated", self.name)
        return self._result(symbol, "earnings", event)

    def get_news(self, symbol: str, limit: int = 5):
        return self._missing(symbol, "news")
