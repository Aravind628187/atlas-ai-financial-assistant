#!/usr/bin/env python3
"""Read-only provider smoke check. Never prints credentials."""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.services.providers.alpha_vantage_provider import AlphaVantageProvider  # noqa: E402
from app.services.providers.finnhub_provider import FinnhubProvider  # noqa: E402
from app.services.providers.fmp_provider import FMPProvider  # noqa: E402
from app.services.providers.massive_provider import MassiveProvider  # noqa: E402
from app.services.providers.newsapi_provider import NewsAPIProvider  # noqa: E402
from app.services.providers.sec_provider import SECProvider  # noqa: E402
from app.services.providers.twelve_data_provider import TwelveDataProvider  # noqa: E402
from app.services.providers.yfinance_provider import YFinanceProvider  # noqa: E402


def check(name, configured, callback) -> None:
    if not configured:
        print(f"{name:<14} {'NOT_CONFIGURED':<20}")
        return
    started = time.monotonic()
    try:
        result = callback()
        latency = (time.monotonic() - started) * 1000
        timestamp = result.data_as_of.isoformat() if result.data_as_of else "timestamp unavailable"
        available = "data" if result.data is not None else "no data"
        raw_status = result.status.value.upper()
        status = raw_status if raw_status in {"OK", "NOT_CONFIGURED", "INVALID_CREDENTIALS", "RATE_LIMITED", "NOT_ENTITLED", "DEGRADED", "ERROR"} else "DEGRADED"
        print(f"{name:<14} {status:<20} {latency:>7.0f}ms  {available:<8} {timestamp}")
    except Exception as exc:
        latency = (time.monotonic() - started) * 1000
        code = getattr(getattr(exc, "response", None), "status_code", None)
        if code == 401 or (code == 403 and name not in {"Finnhub", "Massive"}):
            status = "INVALID_CREDENTIALS"
        elif code == 429:
            status = "RATE_LIMITED"
        elif code == 403:
            status = "NOT_ENTITLED"
        else:
            status = "ERROR"
        print(f"{name:<14} {status:<20} {latency:>7.0f}ms  {type(exc).__name__}")


def main() -> None:
    generic_finnhub = settings.market_data_api_key if settings.market_data_provider.lower() == "finnhub" else ""
    finnhub_key = settings.finnhub_api_key or generic_finnhub
    print("Provider       Status               Latency  Result   Data timestamp")
    check("Finnhub", bool(finnhub_key), lambda: FinnhubProvider(finnhub_key).get_quote("NVDA"))
    check("FMP", bool(settings.fmp_api_key), lambda: FMPProvider(settings.fmp_api_key).get_quote("NVDA"))
    check("Twelve Data", bool(settings.twelve_data_api_key), lambda: TwelveDataProvider(settings.twelve_data_api_key).get_quote("NVDA"))
    check("AlphaVantage", bool(settings.alpha_vantage_api_key), lambda: AlphaVantageProvider(settings.alpha_vantage_api_key).get_fundamentals("NVDA"))
    check("Massive", bool(settings.massive_api_key), lambda: MassiveProvider(settings.massive_api_key).get_quote("NVDA"))
    check("NewsAPI", bool(settings.news_api_key), lambda: NewsAPIProvider(settings.news_api_key).get_news("NVDA", 1))
    check("SEC EDGAR", bool(settings.sec_user_agent), lambda: SECProvider().get_filings("NVDA", ("10-Q",), 1))
    check("yfinance", True, lambda: YFinanceProvider().get_quote("NVDA"))


if __name__ == "__main__":
    main()
