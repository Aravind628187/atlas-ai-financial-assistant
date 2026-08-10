"""Cached U.S. large-cap ranking with an explicitly non-live seed fallback."""
from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings

UTC = dt.timezone.utc
RANKING_CACHE_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class TopCompany:
    symbol: str
    name: str
    market_cap: float | None = None


@dataclass(frozen=True, slots=True)
class TopCompaniesRanking:
    companies: tuple[TopCompany, ...]
    source: str
    retrieved_at: dt.datetime
    is_live: bool


# Reference ordering only. No market-cap values are fabricated, and responses
# identify this source as a fallback seed rather than a current/live ranking.
FALLBACK_SEED: tuple[TopCompany, ...] = (
    TopCompany("NVDA", "NVIDIA"), TopCompany("AAPL", "Apple"),
    TopCompany("GOOGL", "Alphabet"), TopCompany("MSFT", "Microsoft"),
    TopCompany("AMZN", "Amazon"), TopCompany("AVGO", "Broadcom"),
    TopCompany("SPCX", "SpaceX"), TopCompany("META", "Meta Platforms"),
    TopCompany("TSLA", "Tesla"), TopCompany("BRK-B", "Berkshire Hathaway"),
    TopCompany("LLY", "Eli Lilly"), TopCompany("MU", "Micron Technology"),
    TopCompany("JPM", "JPMorgan Chase"), TopCompany("WMT", "Walmart"),
    TopCompany("AMD", "Advanced Micro Devices"), TopCompany("ORCL", "Oracle"),
    TopCompany("XOM", "Exxon Mobil"), TopCompany("COST", "Costco"),
    TopCompany("BAC", "Bank of America"), TopCompany("NFLX", "Netflix"),
    TopCompany("HD", "Home Depot"), TopCompany("PG", "Procter & Gamble"),
    TopCompany("JNJ", "Johnson & Johnson"), TopCompany("ABBV", "AbbVie"),
    TopCompany("KO", "Coca-Cola"),
)


def normalize_top_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if symbol in {"BRK.B", "BRK/B", "BRK-B"}:
        return "BRK-B"
    return symbol


def share_class_key(symbol: str, name: str = "") -> str:
    clean = normalize_top_symbol(symbol)
    if clean in {"GOOG", "GOOGL"} or "alphabet" in name.lower():
        return "ALPHABET"
    if clean in {"BRK-B", "BRK.B"} or "berkshire hathaway" in name.lower():
        return "BERKSHIRE"
    return clean


class TopCompaniesService:
    def __init__(self, client=None, cache_seconds: int = RANKING_CACHE_SECONDS):
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds)
        self.cache_seconds = cache_seconds
        self._cached: TopCompaniesRanking | None = None
        self._cache_until = 0.0
        self._lock = threading.Lock()

    def _fetch_fmp(self) -> tuple[TopCompany, ...]:
        if not settings.fmp_api_key:
            raise RuntimeError("FMP is not configured")
        response = self.client.get(
            "https://financialmodelingprep.com/stable/company-screener",
            params={
                "country": "US", "isActivelyTrading": "true", "limit": 100,
                "apikey": settings.fmp_api_key,
            },
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, list):
            raise RuntimeError("FMP screener did not return a list")
        allowed_exchanges = {"NASDAQ", "NYSE", "AMEX"}
        rows: list[TopCompany] = []
        for row in payload:
            if not isinstance(row, dict):
                continue
            symbol = normalize_top_symbol(row.get("symbol", ""))
            name = str(row.get("companyName") or row.get("name") or symbol).strip()
            exchange = str(row.get("exchangeShortName") or row.get("exchange") or "").upper()
            try:
                market_cap = float(row.get("marketCap"))
            except (TypeError, ValueError):
                continue
            if row.get("isEtf") is True or row.get("isFund") is True:
                continue
            if not symbol or market_cap <= 0 or (exchange and exchange not in allowed_exchanges):
                continue
            rows.append(TopCompany(symbol, name, market_cap))
        rows.sort(key=lambda company: company.market_cap or 0, reverse=True)
        if not rows:
            raise RuntimeError("FMP screener returned no usable U.S. equities")
        return tuple(rows)

    @staticmethod
    def _deduplicate(companies: tuple[TopCompany, ...]) -> tuple[TopCompany, ...]:
        output: list[TopCompany] = []
        seen: set[str] = set()
        for company in companies:
            key = share_class_key(company.symbol, company.name)
            if key in seen:
                continue
            seen.add(key)
            symbol = "GOOGL" if key == "ALPHABET" else normalize_top_symbol(company.symbol)
            output.append(TopCompany(symbol, company.name, company.market_cap))
        return tuple(output)

    def get_top(self, count: int = 15) -> TopCompaniesRanking:
        requested = max(1, min(int(count), 25))
        now = time.monotonic()
        with self._lock:
            if self._cached and now < self._cache_until and len(self._cached.companies) >= requested:
                return TopCompaniesRanking(self._cached.companies[:requested], self._cached.source,
                                           self._cached.retrieved_at, self._cached.is_live)
        retrieved_at = dt.datetime.now(UTC)
        try:
            companies = self._deduplicate(self._fetch_fmp())
            source, is_live = "fmp", True
        except Exception:
            companies = self._deduplicate(FALLBACK_SEED)
            source, is_live = "fallback_seed", False
        ranking = TopCompaniesRanking(companies[:25], source, retrieved_at, is_live)
        with self._lock:
            self._cached = ranking
            self._cache_until = time.monotonic() + self.cache_seconds
        return TopCompaniesRanking(ranking.companies[:requested], source, retrieved_at, is_live)

    def companies_for_symbols(self, symbols: list[str]) -> list[TopCompany]:
        lookup: dict[str, TopCompany] = {normalize_top_symbol(c.symbol): c for c in FALLBACK_SEED}
        with self._lock:
            if self._cached:
                lookup.update({normalize_top_symbol(c.symbol): c for c in self._cached.companies})
        return [lookup.get(normalize_top_symbol(symbol), TopCompany(normalize_top_symbol(symbol), normalize_top_symbol(symbol)))
                for symbol in symbols]

    def clear_cache(self) -> None:
        with self._lock:
            self._cached = None
            self._cache_until = 0.0


top_companies_service = TopCompaniesService()
