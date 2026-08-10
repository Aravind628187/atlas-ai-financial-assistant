"""Central, typed, cached and fail-closed financial data gateway."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import uuid
from dataclasses import replace
from app.config import settings
from app.services.data_freshness import Freshness, market_session_status, quote_freshness
from app.services.providers.base import DataStatus, EarningsData, FinancialDataResult, QuoteData
from app.services.providers.finnhub_provider import FinnhubProvider
from app.services.providers.yfinance_provider import YFinanceProvider
from app.services.providers.sec_provider import SECProvider
from app.services.financial_data_router import FinancialDataRouter
from app.services.runtime_state import reliability_telemetry


logger = logging.getLogger("atlas.data_gateway")
UTC = dt.timezone.utc


class TTLCache:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, float, FinancialDataResult]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> FinancialDataResult | None:
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            expires, _, value = entry
            if time.monotonic() >= expires:
                return None
            return replace(value, cache_hit=True)

    def get_last_verified(self, key: str, max_age_seconds: int) -> FinancialDataResult | None:
        """Return a previously accepted result, explicitly marked as historical cache."""
        with self._lock:
            entry = self._entries.get(key)
            if not entry:
                return None
            _, stored_at, value = entry
            if time.monotonic() - stored_at > max_age_seconds:
                self._entries.pop(key, None)
                return None
            if value.data is None or value.status in {
                DataStatus.UNAVAILABLE, DataStatus.ERROR, DataStatus.CONFLICTING_DATA,
                DataStatus.INVALID_CREDENTIALS, DataStatus.RATE_LIMITED, DataStatus.NOT_ENTITLED,
            }:
                return None
            if isinstance(value.data, EarningsData) and value.data.status == "unverified":
                return None
            verification = {
                **(value.verification or {}),
                "cached_verified": True,
                "current_providers_unavailable": True,
                "original_status": value.status.value,
            }
            return replace(
                value, status=DataStatus.STALE, cache_hit=True, is_realtime=False,
                is_delayed=True, is_stale=True, verification=verification,
            )

    def set(self, key: str, value: FinancialDataResult, ttl: int) -> None:
        with self._lock:
            now = time.monotonic()
            self._entries[key] = (now + ttl, now, value)

    def state(self) -> dict[str, int]:
        now = time.monotonic()
        with self._lock:
            active = sum(1 for expires, _, _ in self._entries.values() if expires > now)
            return {"active_entries": active, "total_entries": len(self._entries)}


def _provider(name: str, api_key: str):
    normalized = (name or "").strip().lower()
    if normalized in {"finnhub", "primary", "professional"} and api_key:
        return FinnhubProvider(api_key)
    if normalized in {"yfinance", "yahoo", ""}:
        return YFinanceProvider()
    logger.warning("Unsupported market provider %s; it will be marked unavailable", normalized)
    return None


class FinancialDataGateway:
    """The only production-facing entry point for financial data."""

    def __init__(self, primary=None, secondary=None) -> None:
        self._legacy_injected = primary is not None or secondary is not None
        self.router = None if self._legacy_injected else FinancialDataRouter()
        configured_primary = _provider(settings.market_data_provider, settings.market_data_api_key)
        if self.router:
            ordered = self.router._ordered("quote")
            self.primary = ordered[0][1] if ordered else YFinanceProvider()
        else:
            self.primary = primary or configured_primary or YFinanceProvider()
        configured_secondary = _provider(
            settings.secondary_market_data_provider, settings.secondary_market_data_api_key
        ) if settings.secondary_market_data_provider else None
        self.secondary = secondary if self._legacy_injected else (self.router._ordered("quote")[1][1] if self.router and len(self.router._ordered("quote")) > 1 else None)
        if self._legacy_injected and self.primary.name != "yfinance" and self.secondary is None:
            self.secondary = YFinanceProvider()
        self.cache = TTLCache()
        self.sec = SECProvider()
        self.sec_last_success: dt.datetime | None = None
        self.sec_last_failure: dt.datetime | None = None
        self.last_success: dt.datetime | None = None
        self.last_failure: dict | None = None
        self.rejected_disagreements = 0
        self._lock = threading.Lock()
        self._request_locks: dict[str, threading.Lock] = {}
        self._request_locks_guard = threading.Lock()

    @property
    def providers(self):
        return [item for item in (self.primary, self.secondary) if item is not None]

    def _unavailable(self, symbol: str, operation: str, error: str = "No provider returned verified data"):
        return FinancialDataResult(
            status=DataStatus.UNAVAILABLE, source="none", source_type=operation,
            retrieved_at=dt.datetime.now(UTC), data_as_of=None, symbol=symbol.upper(),
            data=None, is_realtime=False, is_delayed=False, error=error,
        )

    def _call(self, provider, operation: str, symbol: str, *args) -> FinancialDataResult:
        started = time.monotonic()
        request_id = uuid.uuid4().hex[:16]
        last_error: Exception | None = None
        for attempt in range(max(1, settings.provider_max_retries)):
            try:
                result = getattr(provider, operation)(symbol, *args)
                latency = round((time.monotonic() - started) * 1000, 2)
                if result.status != DataStatus.UNAVAILABLE and result.data is not None:
                    with self._lock:
                        self.last_success = result.retrieved_at
                    self._log_fetch(request_id, provider.name, operation, symbol, result.status.value, latency, None)
                    return result
                last_error = RuntimeError(result.error or "empty provider response")
            except Exception as exc:  # provider failures never escape into Telegram
                last_error = exc
                if attempt + 1 < max(1, settings.provider_max_retries):
                    time.sleep(min(0.25 * (2 ** attempt), 1.0))
        latency = round((time.monotonic() - started) * 1000, 2)
        error_type = type(last_error).__name__ if last_error else "Unavailable"
        with self._lock:
            self.last_failure = {
                "provider": provider.name, "operation": operation, "symbol": symbol.upper(),
                "error_type": error_type, "at": dt.datetime.now(UTC).isoformat(),
            }
        self._log_fetch(request_id, provider.name, operation, symbol, "unavailable", latency, error_type)
        logger.warning("provider_failure provider=%s operation=%s symbol=%s error=%s",
                       provider.name, operation, symbol.upper(), error_type)
        return self._unavailable(symbol, operation, error_type)

    @staticmethod
    def _log_fetch(request_id, provider, operation, symbol, status, latency, error_type, cache_hit=False):
        try:
            from app.database import get_session
            from app.models import DataFetchLog
            with get_session() as db:
                db.add(DataFetchLog(
                    request_id=request_id, provider=provider, operation=operation,
                    symbol=symbol.upper(), status=status, latency_ms=latency,
                    cache_hit=cache_hit, error_type=error_type,
                ))
        except Exception:
            logger.debug("Unable to persist provider metric", exc_info=True)

    def _cached_fetch(self, operation: str, symbol: str, ttl: int, *args) -> FinancialDataResult:
        clean = symbol.strip().upper()
        key = f"{operation}:{clean}:{args}"
        cached = self.cache.get(key)
        if cached:
            self._log_fetch(uuid.uuid4().hex[:16], cached.source, operation, clean,
                            cached.status.value, 0.0, None, cache_hit=True)
            return cached
        last_verified = self.cache.get_last_verified(key, self._retention_seconds(operation))
        failures: list[str] = []
        for provider in self.providers:
            result = self._call(provider, operation, clean, *args)
            if result.status != DataStatus.UNAVAILABLE and result.data is not None:
                self.cache.set(key, result, ttl)
                return result
            failures.append(f"{provider.name}:{result.error or 'unavailable'}")
        if last_verified:
            reliability_telemetry.increment("cached_financial_data_used")
            return last_verified
        return self._unavailable(clean, operation, "; ".join(failures))

    @staticmethod
    def _retention_seconds(operation: str) -> int:
        if operation == "get_quote":
            return settings.verified_quote_cache_seconds
        if operation == "get_news":
            return min(settings.verified_reference_cache_seconds, 86400)
        return settings.verified_reference_cache_seconds

    def _request_lock(self, key: str) -> threading.Lock:
        with self._request_locks_guard:
            return self._request_locks.setdefault(key, threading.Lock())

    def _routed_fetch(self, intent: str, operation: str, symbol: str, ttl: int, *args, verify: bool = False) -> FinancialDataResult:
        clean = symbol.strip().upper()
        key = f"{operation}:{clean}:{args}:verify={verify}"
        # Coalesce concurrent identical requests. The second caller rechecks
        # the cache after the first completes instead of spending more quota.
        with self._request_lock(key):
            return self._routed_fetch_locked(intent, operation, clean, key, ttl, *args, verify=verify)

    def _routed_fetch_locked(self, intent: str, operation: str, clean: str, key: str,
                             ttl: int, *args, verify: bool = False) -> FinancialDataResult:
        cached = self.cache.get(key)
        if cached:
            self._log_fetch(uuid.uuid4().hex[:16], cached.source, operation, clean, cached.status.value, 0.0, None, cache_hit=True)
            return cached
        last_verified = self.cache.get_last_verified(key, self._retention_seconds(operation))
        result = self.router.fetch(intent, clean, *args, verify=verify) if self.router else self._cached_fetch(operation, clean, ttl, *args)
        self._log_fetch(uuid.uuid4().hex[:16], result.source, operation, clean, result.status.value, 0.0, result.error)
        if result.data is not None and result.status not in {DataStatus.UNAVAILABLE, DataStatus.ERROR, DataStatus.CONFLICTING_DATA}:
            self.cache.set(key, result, ttl)
            with self._lock:
                self.last_success = result.retrieved_at
        else:
            with self._lock:
                self.last_failure = {"provider": result.source, "operation": operation, "symbol": clean,
                                     "error_type": result.error, "at": dt.datetime.now(UTC).isoformat()}
            if result.status != DataStatus.CONFLICTING_DATA and last_verified:
                reliability_telemetry.increment("cached_financial_data_used")
                return last_verified
        return result

    def get_quote(self, symbol: str, verify: bool = True) -> FinancialDataResult:
        result = self._routed_fetch("quote", "get_quote", symbol, settings.quote_cache_ttl_seconds,
                                    verify=verify and settings.financial_provider_verify_critical) if self.router else self._cached_fetch("get_quote", symbol, settings.quote_cache_ttl_seconds)
        if result.status == DataStatus.UNAVAILABLE:
            return result
        freshness = quote_freshness(
            result.data_as_of, is_realtime=result.is_realtime, is_delayed=result.is_delayed,
            stale_after_seconds=settings.quote_stale_after_seconds,
        )
        result.is_stale = freshness == Freshness.STALE
        result.freshness = freshness.value
        result.market_status = market_session_status().value
        result.status = DataStatus.STALE if result.is_stale else result.status
        result.verification = {
            **(result.verification or {}),
            "freshness": freshness.value,
            "market_session": market_session_status().value,
        }
        if result.is_stale:
            self._log_fetch(uuid.uuid4().hex[:16], result.source, "get_quote", symbol.upper(),
                            "stale", 0.0, None, cache_hit=result.cache_hit)

        if not self.router and verify and self.secondary and not result.cache_hit:
            secondary = self._call(self.secondary, "get_quote", symbol.upper())
            left = result.data.price if isinstance(result.data, QuoteData) else None
            right = secondary.data.price if isinstance(secondary.data, QuoteData) else None
            if left and right:
                difference = abs(left - right) / max(abs(left), abs(right)) * 100
                result.verification.update({
                    "secondary_source": secondary.source,
                    "secondary_price": right,
                    "difference_pct": round(difference, 4),
                })
                if difference > settings.quote_verification_tolerance_pct:
                    result.status = DataStatus.UNAVAILABLE
                    result.data = None
                    result.error = "Configured providers disagree beyond tolerance"
                    result.verification["disagreement"] = True
                    self.rejected_disagreements += 1
        return result

    def get_profile(self, symbol: str) -> FinancialDataResult:
        return self._routed_fetch("profile", "get_profile", symbol, settings.fundamentals_cache_ttl_seconds) if self.router else self._cached_fetch("get_profile", symbol, settings.fundamentals_cache_ttl_seconds)

    def get_fundamentals(self, symbol: str) -> FinancialDataResult:
        return self._routed_fetch("fundamentals", "get_fundamentals", symbol, settings.fundamentals_cache_ttl_seconds,
                                  verify=settings.financial_provider_verify_critical) if self.router else self._cached_fetch("get_fundamentals", symbol, settings.fundamentals_cache_ttl_seconds)

    def get_news(self, symbol: str, limit: int = 5) -> FinancialDataResult:
        return self._routed_fetch("news", "get_news", symbol, settings.news_cache_ttl_seconds, limit) if self.router else self._cached_fetch("get_news", symbol, settings.news_cache_ttl_seconds, limit)

    def get_market_news(self, limit: int = 6) -> FinancialDataResult:
        return self._routed_fetch("market_news", "get_market_news", "MARKET", settings.news_cache_ttl_seconds, limit) if self.router else self._unavailable("MARKET", "market_news")

    def get_earnings(self, symbol: str) -> FinancialDataResult:
        return self._routed_fetch("earnings", "get_earnings", symbol, settings.fundamentals_cache_ttl_seconds) if self.router else self._cached_fetch("get_earnings", symbol, settings.fundamentals_cache_ttl_seconds)

    def get_history(self, symbol: str, period: str = "1mo") -> FinancialDataResult:
        return self._routed_fetch("history", "get_history", symbol, settings.fundamentals_cache_ttl_seconds, period) if self.router else self._cached_fetch("get_history", symbol, settings.fundamentals_cache_ttl_seconds, period)

    def get_filings(self, symbol: str, forms: tuple[str, ...] = ("10-Q", "10-K", "8-K"), limit: int = 5) -> FinancialDataResult:
        clean = symbol.strip().upper()
        key = f"get_filings:{clean}:{forms}:{limit}"
        cached = self.cache.get(key)
        if cached:
            return cached
        last_verified = self.cache.get_last_verified(key, settings.verified_reference_cache_seconds)
        result = self._call(self.sec, "get_filings", clean, forms, limit)
        if result.status != DataStatus.UNAVAILABLE and result.data is not None:
            self.cache.set(key, result, settings.sec_cache_ttl_seconds)
            self.sec_last_success = result.retrieved_at
        else:
            self.sec_last_failure = dt.datetime.now(UTC)
            if last_verified:
                reliability_telemetry.increment("cached_financial_data_used")
                return last_verified
        return result

    def health(self) -> dict:
        health = {
            "primary_provider": getattr(self.primary, "name", "unavailable"),
            "secondary_provider": getattr(self.secondary, "name", None),
            "last_successful_fetch": self.last_success.isoformat() if self.last_success else None,
            "last_provider_failure": self.last_failure,
            "provider_disagreements": self.rejected_disagreements,
            "cache": self.cache.state(),
            "filings_provider": self.sec.name,
        }
        if self.router:
            router_health = self.router.health()
            router_health["providers"]["sec_edgar"] = {
                "provider": "sec_edgar", "configured": bool(settings.sec_user_agent), "status": "ok" if self.sec_last_success else "degraded",
                "last_success_at": self.sec_last_success.isoformat() if self.sec_last_success else None,
                "last_failure_at": self.sec_last_failure.isoformat() if self.sec_last_failure else None,
                "last_success": self.sec_last_success.isoformat() if self.sec_last_success else None,
                "last_failure": self.sec_last_failure.isoformat() if self.sec_last_failure else None,
                "latency_ms": None, "failure_reason": None, "failure_category": None,
            }
            health["router"] = router_health
        return health


gateway = FinancialDataGateway()
