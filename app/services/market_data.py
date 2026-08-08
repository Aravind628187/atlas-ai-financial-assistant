"""
Live market data via yfinance (free, no API key required — chosen so the
project runs the moment a user drops in only a Gemini key + bot token, per
the assignment's request). Swap in Finnhub / Alpha Vantage / Polygon here
if you have a paid key; the rest of the app only depends on this module's
function signatures, not on yfinance itself.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import yfinance as yf

logger = logging.getLogger("atlas.market")


@dataclass
class Quote:
    symbol: str
    price: float | None
    change: float | None
    change_pct: float | None
    prev_close: float | None
    currency: str | None
    name: str | None


def get_quote(symbol: str) -> Quote | None:
    try:
        t = yf.Ticker(symbol)
        fast = t.fast_info
        price = fast.get("lastPrice") or fast.get("last_price")
        prev_close = fast.get("previousClose") or fast.get("previous_close")
        if price is None:
            return None
        change = None if prev_close is None else price - prev_close
        change_pct = None if not prev_close else (change / prev_close) * 100
        name = None
        try:
            name = t.info.get("shortName")
        except Exception:  # noqa: BLE001 — .info is best-effort, can be slow/flaky
            pass
        return Quote(
            symbol=symbol.upper(),
            price=round(price, 2) if price else None,
            change=round(change, 2) if change is not None else None,
            change_pct=round(change_pct, 2) if change_pct is not None else None,
            prev_close=round(prev_close, 2) if prev_close else None,
            currency=fast.get("currency"),
            name=name,
        )
    except Exception:
        logger.exception("get_quote failed for %s", symbol)
        return None


def get_company_profile(symbol: str) -> dict:
    try:
        t = yf.Ticker(symbol)
        info = t.info or {}
        return {
            "symbol": symbol.upper(),
            "name": info.get("shortName") or info.get("longName"),
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "summary": info.get("longBusinessSummary"),
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "dividend_yield": info.get("dividendYield"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "employees": info.get("fullTimeEmployees"),
            "website": info.get("website"),
            "revenue_growth": info.get("revenueGrowth"),
            "profit_margin": info.get("profitMargins"),
        }
    except Exception:
        logger.exception("get_company_profile failed for %s", symbol)
        return {"symbol": symbol.upper()}


def get_recent_news(symbol: str, limit: int = 5) -> list[dict]:
    try:
        t = yf.Ticker(symbol)
        items = t.news or []
        out = []
        for it in items[:limit]:
            content = it.get("content", it)  # yfinance schema has shifted across versions
            out.append(
                {
                    "title": content.get("title") or it.get("title"),
                    "publisher": (content.get("provider") or {}).get("displayName")
                    if isinstance(content.get("provider"), dict)
                    else it.get("publisher"),
                    "link": (content.get("canonicalUrl") or {}).get("url") if isinstance(content.get("canonicalUrl"), dict) else it.get("link"),
                    "published": content.get("pubDate") or it.get("providerPublishTime"),
                }
            )
        return [n for n in out if n.get("title")]
    except Exception:
        logger.exception("get_recent_news failed for %s", symbol)
        return []


def get_earnings_calendar(symbol: str) -> dict:
    try:
        t = yf.Ticker(symbol)
        cal = t.calendar
        if isinstance(cal, dict):
            return {"symbol": symbol.upper(), "earnings_date": cal.get("Earnings Date")}
        return {"symbol": symbol.upper(), "earnings_date": None}
    except Exception:
        logger.exception("get_earnings_calendar failed for %s", symbol)
        return {"symbol": symbol.upper(), "earnings_date": None}


def compare_symbols(symbols: list[str]) -> list[dict]:
    results = []
    for s in symbols:
        quote = get_quote(s)
        profile = get_company_profile(s)
        profile["quote"] = quote.__dict__ if quote else None
        results.append(profile)
    return results
