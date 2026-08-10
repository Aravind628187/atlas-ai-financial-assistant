"""Backward-compatible facade over the central financial data gateway.

New code should use ``financial_data_gateway.gateway`` directly. Keeping these
functions preserves existing scheduler/API integrations and third-party imports.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from app.services.financial_data_gateway import gateway
from app.services.providers.base import EarningsData, QuoteData


@dataclass
class Quote:
    symbol: str
    price: float | None
    change: float | None
    change_pct: float | None
    prev_close: float | None
    currency: str | None
    name: str | None
    source: str | None = None
    data_as_of: str | None = None
    freshness: str | None = None


def get_quote(symbol: str) -> Quote | None:
    result = gateway.get_quote(symbol)
    data = result.data
    if not isinstance(data, QuoteData):
        return None
    return Quote(
        symbol=data.symbol, price=data.price, change=data.change,
        change_pct=data.change_pct, prev_close=data.previous_close,
        currency=data.currency, name=data.name, source=result.source,
        data_as_of=result.data_as_of.isoformat() if result.data_as_of else None,
        freshness=(result.verification or {}).get("freshness"),
    )


def get_company_profile(symbol: str) -> dict:
    profile = gateway.get_profile(symbol)
    fundamentals = gateway.get_fundamentals(symbol)
    output = {"symbol": symbol.upper(), "source": profile.source}
    if profile.data:
        output.update(asdict(profile.data))
    if fundamentals.data:
        output.update(asdict(fundamentals.data))
    return output


def get_recent_news(symbol: str, limit: int = 5) -> list[dict]:
    result = gateway.get_news(symbol, limit)
    return [
        {
            "news_id": item.news_id, "title": item.headline, "publisher": item.publisher,
            "published": item.published_at.isoformat() if item.published_at else None,
            "link": item.url, "summary": item.summary, "source": result.source,
        }
        for item in (result.data or [])
    ]


def get_earnings_calendar(symbol: str) -> dict:
    result = gateway.get_earnings(symbol)
    event = result.data if isinstance(result.data, EarningsData) else None
    displayable = bool(event and event.earnings_date and event.status in {"confirmed", "estimated"})
    return {
        "symbol": symbol.upper(),
        "earnings_date": event.earnings_date.isoformat() if displayable else None,
        "status": event.status if event else "unverified",
        "source": (event.source or result.source) if event else result.source,
        "verified_with": event.verified_with if event else None,
        "data_as_of": result.data_as_of.isoformat() if result.data_as_of else None,
    }


def compare_symbols(symbols: list[str]) -> list[dict]:
    output = []
    for symbol in symbols:
        row = get_company_profile(symbol)
        quote = get_quote(symbol)
        row["quote"] = asdict(quote) if quote else None
        output.append(row)
    return output
