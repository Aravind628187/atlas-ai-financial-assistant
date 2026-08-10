"""Best-effort yfinance fallback, always labelled delayed/latest available."""
from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import fields
from typing import Any

import yfinance as yf

from app.services.providers.base import (
    CompanyProfile, DataStatus, EarningsData, FinancialDataResult, FundamentalsData,
    HistoricalData, HistoricalPoint, NewsItem, QuoteData, has_fundamental_values,
)


UTC = dt.timezone.utc


def _utcnow() -> dt.datetime:
    return dt.datetime.now(UTC)


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
        return parsed if parsed == parsed else None
    except (TypeError, ValueError):
        return None


def _datetime(value: Any) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
    if hasattr(value, "to_pydatetime"):
        return _datetime(value.to_pydatetime())
    if isinstance(value, (int, float)):
        return dt.datetime.fromtimestamp(value, tz=UTC)
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except ValueError:
        return None


class YFinanceProvider:
    name = "yfinance"

    def _result(self, *, symbol: str, data: Any, data_as_of: dt.datetime | None = None,
                source_type: str = "market_data", status: DataStatus = DataStatus.OK) -> FinancialDataResult:
        return FinancialDataResult(
            status=status, source=self.name, source_type=source_type,
            retrieved_at=_utcnow(), data_as_of=data_as_of, symbol=symbol.upper(), data=data,
            is_realtime=False, is_delayed=True,
        )

    def get_quote(self, symbol: str) -> FinancialDataResult:
        ticker = yf.Ticker(symbol)
        history = ticker.history(period="5d", interval="1m", auto_adjust=False)
        if history is None or history.empty:
            history = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if history is None or history.empty:
            return self._result(symbol=symbol, data=None, status=DataStatus.UNAVAILABLE)
        valid = history.dropna(subset=["Close"])
        if valid.empty:
            return self._result(symbol=symbol, data=None, status=DataStatus.UNAVAILABLE)
        price = _number(valid["Close"].iloc[-1])
        as_of = _datetime(valid.index[-1])
        fast = ticker.fast_info
        previous = _number(fast.get("previousClose") or fast.get("previous_close"))
        if previous is None and len(valid) > 1:
            previous = _number(valid["Close"].iloc[-2])
        change = price - previous if price is not None and previous is not None else None
        pct = change / previous * 100 if change is not None and previous else None
        quote = QuoteData(
            symbol=symbol.upper(), price=price, previous_close=previous, change=change,
            change_pct=pct, currency=fast.get("currency"), market_state=None,
        )
        return self._result(symbol=symbol, data=quote, data_as_of=as_of)

    def _info(self, symbol: str) -> dict[str, Any]:
        return yf.Ticker(symbol).info or {}

    def get_profile(self, symbol: str) -> FinancialDataResult:
        info = self._info(symbol)
        profile = CompanyProfile(
            symbol=symbol.upper(), name=info.get("shortName") or info.get("longName"),
            sector=info.get("sector"), industry=info.get("industry"),
            summary=info.get("longBusinessSummary"), website=info.get("website"),
        )
        available = any((profile.name, profile.sector, profile.industry, profile.summary))
        return self._result(symbol=symbol, data=profile if available else None,
                            status=DataStatus.OK if available else DataStatus.UNAVAILABLE)

    def get_fundamentals(self, symbol: str) -> FinancialDataResult:
        info = self._info(symbol)
        dividend_rate = _number(info.get("dividendRate"))
        current_price = _number(info.get("currentPrice") or info.get("regularMarketPrice"))
        # yfinance/Yahoo has changed dividendYield units across payload versions.
        # Derive an unambiguous fraction from annual dividend / price or omit it.
        dividend_yield = dividend_rate / current_price if dividend_rate is not None and current_price else None
        data = FundamentalsData(
            symbol=symbol.upper(), market_cap=_number(info.get("marketCap")),
            trailing_pe=_number(info.get("trailingPE")), forward_pe=_number(info.get("forwardPE")),
            revenue=_number(info.get("totalRevenue")), profit_margin=_number(info.get("profitMargins")),
            revenue_growth=_number(info.get("revenueGrowth")),
            fifty_two_week_high=_number(info.get("fiftyTwoWeekHigh")),
            fifty_two_week_low=_number(info.get("fiftyTwoWeekLow")),
            dividend_yield=dividend_yield, revenue_period="ttm", margin_period="ttm",
            unit_notes={"dividend_yield": "derived from dividendRate/currentPrice"},
            dividend_yield_forward=dividend_yield, revenue_growth_period="quarterly_yoy",
            currency=info.get("financialCurrency") or info.get("currency"),
            metric_definitions={"trailing_pe": "trailing", "forward_pe": "forward", "revenue": "ttm",
                                "profit_margin": "ttm_net", "revenue_growth": "quarterly_yoy",
                                "dividend_yield": "forward", "market_cap": "current"},
        )
        available = has_fundamental_values(data)
        return self._result(symbol=symbol, data=data if available else None,
                            data_as_of=None, status=DataStatus.OK if available else DataStatus.UNAVAILABLE)

    def get_news(self, symbol: str, limit: int = 5) -> FinancialDataResult:
        rows = yf.Ticker(symbol).news or []
        items: list[NewsItem] = []
        for row in rows[:limit]:
            content = row.get("content", row)
            headline = content.get("title") or row.get("title")
            if not headline:
                continue
            provider = content.get("provider") or {}
            canonical = content.get("canonicalUrl") or {}
            url = canonical.get("url") if isinstance(canonical, dict) else row.get("link")
            published = _datetime(content.get("pubDate") or row.get("providerPublishTime"))
            items.append(NewsItem(
                news_id=hashlib.sha256(f"{headline}|{url}".encode()).hexdigest()[:16],
                headline=headline,
                publisher=provider.get("displayName") if isinstance(provider, dict) else row.get("publisher"),
                published_at=published, url=url, summary=content.get("summary"),
            ))
        as_of = max((item.published_at for item in items if item.published_at), default=None)
        return self._result(symbol=symbol, data=items, data_as_of=as_of, source_type="news",
                            status=DataStatus.OK if items else DataStatus.UNAVAILABLE)

    def get_earnings(self, symbol: str) -> FinancialDataResult:
        calendar = yf.Ticker(symbol).calendar
        raw = calendar.get("Earnings Date") if isinstance(calendar, dict) else None
        if isinstance(raw, (list, tuple)):
            raw = raw[0] if raw else None
        date = _datetime(raw)
        return self._result(symbol=symbol, data=EarningsData(symbol.upper(), date) if date else None,
                            data_as_of=None, status=DataStatus.OK if date else DataStatus.UNAVAILABLE)

    def get_history(self, symbol: str, period: str) -> FinancialDataResult:
        frame = yf.Ticker(symbol).history(period=period, interval="1d", auto_adjust=False)
        points: list[HistoricalPoint] = []
        if frame is not None:
            for index, row in frame.dropna(subset=["Close"]).iterrows():
                stamp = _datetime(index)
                if stamp:
                    points.append(HistoricalPoint(
                        timestamp=stamp, open=_number(row.get("Open")), high=_number(row.get("High")),
                        low=_number(row.get("Low")), close=_number(row.get("Close")),
                        volume=int(row.get("Volume")) if _number(row.get("Volume")) is not None else None,
                    ))
        data = HistoricalData(symbol.upper(), points)
        return self._result(symbol=symbol, data=data if points else None,
                            data_as_of=points[-1].timestamp if points else None,
                            status=DataStatus.OK if points else DataStatus.UNAVAILABLE)
