"""Intent-aware provider selection, health tracking, failover and verification."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import settings
from app.services.data_freshness import Freshness, market_session_status, quote_freshness
from app.services.news_relevance import filter_company_news
from app.services.providers.alpha_vantage_provider import AlphaVantageProvider
from app.services.providers.base import DataStatus, EarningsData, FinancialDataResult, FundamentalsData, NewsItem, QuoteData, normalize_earnings_datetime, unavailable_result
from app.services.providers.finnhub_provider import FinnhubProvider
from app.services.providers.fmp_provider import FMPProvider
from app.services.providers.massive_provider import MassiveProvider
from app.services.providers.newsapi_provider import NewsAPIProvider
from app.services.providers.rss_provider import RSSNewsProvider
from app.services.providers.twelve_data_provider import TwelveDataProvider
from app.services.providers.yfinance_provider import YFinanceProvider
from app.services.runtime_state import reliability_telemetry


logger = logging.getLogger("atlas.financial_router")
UTC = dt.timezone.utc
FAILURE_THRESHOLD = 3
TRANSIENT_CIRCUIT_SECONDS = 120
QUOTA_CIRCUIT_SECONDS = 1800
ENTITLEMENT_CIRCUIT_SECONDS = 3600
EARNINGS_MAX_UNVERIFIED_DAYS = 120
EARNINGS_AGREEMENT_DAYS = 3


def current_utc_date() -> dt.date:
    return dt.datetime.now(UTC).date()


@dataclass
class ProviderHealth:
    configured: bool
    status: str
    consecutive_failures: int = 0
    last_success_at: dt.datetime | None = None
    last_failure_at: dt.datetime | None = None
    latency_ms: float | None = None
    failure_reason: str | None = None
    circuit_until: float = 0.0
    cooldown_until: dt.datetime | None = None


def _configured(provider) -> bool:
    return bool(getattr(provider, "configured", True))


def _generic_key(provider_name: str) -> str:
    if settings.market_data_provider.lower() == provider_name:
        return settings.market_data_api_key
    if settings.secondary_market_data_provider.lower() == provider_name:
        return settings.secondary_market_data_api_key
    return ""


class FinancialDataRouter:
    ROUTES = {
        "quote": ["finnhub", "twelve_data", "yfinance"],
        "profile": ["finnhub", "fmp", "alpha_vantage", "yfinance"],
        "fundamentals": ["fmp", "alpha_vantage", "finnhub", "yfinance"],
        "history": ["twelve_data", "fmp", "alpha_vantage", "yfinance"],
        "earnings": ["fmp", "finnhub", "alpha_vantage"],
        "news": ["newsapi", "finnhub", "rss", "yfinance"],
        "market_news": ["newsapi"],
    }
    METHODS = {
        "quote": "get_quote", "profile": "get_profile", "fundamentals": "get_fundamentals",
        "history": "get_history", "earnings": "get_earnings", "news": "get_news",
        "market_news": "get_market_news",
    }

    def __init__(self, providers: dict[str, Any] | None = None):
        if providers is None:
            finnhub_key = settings.finnhub_api_key or _generic_key("finnhub")
            providers = {
                "finnhub": FinnhubProvider(finnhub_key) if finnhub_key else None,
                "fmp": FMPProvider(settings.fmp_api_key),
                "twelve_data": TwelveDataProvider(settings.twelve_data_api_key),
                "alpha_vantage": AlphaVantageProvider(settings.alpha_vantage_api_key),
                "newsapi": NewsAPIProvider(settings.news_api_key),
                "massive": MassiveProvider(settings.massive_api_key),
                "rss": RSSNewsProvider(),
                "yfinance": YFinanceProvider(),
            }
        self.providers = providers
        self.health_state: dict[str, ProviderHealth] = {
            name: ProviderHealth(
                configured=provider is not None and _configured(provider),
                status="degraded" if provider is not None and _configured(provider) else "not_configured",
            ) for name, provider in providers.items()
        }
        self.fallback_usage = 0
        self.conflicts = 0
        self._lock = threading.Lock()

    def _ordered(self, intent: str) -> list[tuple[str, Any]]:
        names = list(self.ROUTES[intent])
        if intent == "news" and settings.telegram_mode == "webhook":
            names = ["finnhub", "rss", "yfinance", "newsapi"]
        result = []
        for name in names:
            provider = self.providers.get(name)
            health = self.health_state.get(name)
            if not provider or not health or not health.configured:
                continue
            if health.circuit_until > time.monotonic():
                reliability_telemetry.increment("circuit_breaker_skips")
                continue
            result.append((name, provider))
        return result

    @staticmethod
    def _reason(exc: Exception) -> str:
        if isinstance(exc, httpx.TimeoutException) or isinstance(exc, TimeoutError): return "timeout"
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 429: return "rate_limited"
        if status == 401: return "invalid_credentials"
        if status == 403: return "not_entitled"
        if status and status >= 500: return "upstream_5xx"
        message = str(exc).lower()
        if any(term in message for term in ("rate limit", "frequency", "too many requests")): return "rate_limited"
        if any(term in message for term in ("invalid api key", "invalid token", "api key is invalid")): return "invalid_credentials"
        if any(term in message for term in ("not entitled", "subscription", "premium endpoint")): return "not_entitled"
        return type(exc).__name__

    def _record_success(self, name: str, latency: float):
        with self._lock:
            state = self.health_state[name]
            state.status, state.consecutive_failures = "ok", 0
            state.last_success_at, state.latency_ms = dt.datetime.now(UTC), round(latency, 2)
            state.failure_reason, state.circuit_until, state.cooldown_until = None, 0, None

    def _record_failure(self, name: str, latency: float, reason: str):
        with self._lock:
            state = self.health_state[name]
            state.consecutive_failures += 1
            state.last_failure_at, state.latency_ms, state.failure_reason = dt.datetime.now(UTC), round(latency, 2), reason
            state.status = reason if reason in {"invalid_credentials", "rate_limited", "not_entitled"} else "degraded"
            if state.consecutive_failures >= FAILURE_THRESHOLD or reason in {"invalid_credentials", "rate_limited", "not_entitled"}:
                if reason == "invalid_credentials":
                    state.circuit_until = float("inf")
                    state.cooldown_until = None
                elif reason == "rate_limited":
                    state.circuit_until = time.monotonic() + QUOTA_CIRCUIT_SECONDS
                    state.cooldown_until = dt.datetime.now(UTC) + dt.timedelta(seconds=QUOTA_CIRCUIT_SECONDS)
                elif reason == "not_entitled":
                    state.circuit_until = time.monotonic() + ENTITLEMENT_CIRCUIT_SECONDS
                    state.cooldown_until = dt.datetime.now(UTC) + dt.timedelta(seconds=ENTITLEMENT_CIRCUIT_SECONDS)
                else:
                    state.circuit_until = time.monotonic() + TRANSIENT_CIRCUIT_SECONDS
                    state.cooldown_until = dt.datetime.now(UTC) + dt.timedelta(seconds=TRANSIENT_CIRCUIT_SECONDS)
                state.status = reason if reason in {"invalid_credentials", "rate_limited", "not_entitled"} else "error"

    def _call(self, name: str, provider: Any, intent: str, symbol: str, *args) -> FinancialDataResult:
        method = self.METHODS[intent]
        started = time.monotonic()
        last_error: Exception | None = None
        for attempt in range(max(1, settings.financial_provider_max_retries)):
            try:
                reliability_telemetry.record_upstream_call(name)
                result = getattr(provider, method)(symbol, *args) if intent != "market_news" else getattr(provider, method)(*args)
                latency = (time.monotonic() - started) * 1000
                if result.status not in {DataStatus.UNAVAILABLE, DataStatus.ERROR, DataStatus.NOT_CONFIGURED,
                                         DataStatus.INVALID_CREDENTIALS, DataStatus.RATE_LIMITED,
                                         DataStatus.NOT_ENTITLED} and result.data is not None:
                    self._record_success(name, latency)
                    return result
                returned_reason = {
                    DataStatus.INVALID_CREDENTIALS: "invalid_credentials",
                    DataStatus.RATE_LIMITED: "rate_limited",
                    DataStatus.NOT_ENTITLED: "not_entitled",
                }.get(result.status)
                last_error = RuntimeError(returned_reason or result.error or "empty_result")
                break
            except Exception as exc:
                last_error = exc
                reason = self._reason(exc)
                if reason in {"rate_limited", "invalid_credentials", "not_entitled"} or attempt + 1 >= max(1, settings.financial_provider_max_retries):
                    break
                time.sleep(min(0.2 * (2 ** attempt), 0.8))
        latency = (time.monotonic() - started) * 1000
        message = str(last_error or "unavailable")
        reason = message if message in {"rate_limited", "invalid_credentials", "not_entitled"} else self._reason(last_error or RuntimeError("unavailable"))
        if reason == "rate_limited":
            reliability_telemetry.increment("provider_429_count")
        self._record_failure(name, latency, reason)
        status = {"rate_limited": DataStatus.RATE_LIMITED, "invalid_credentials": DataStatus.INVALID_CREDENTIALS,
                  "not_entitled": DataStatus.NOT_ENTITLED}.get(reason, DataStatus.UNAVAILABLE)
        return unavailable_result(name, symbol, intent, reason, status)

    def fetch(self, intent: str, symbol: str, *args, verify: bool = False) -> FinancialDataResult:
        if intent == "news":
            return self._fetch_company_news(symbol, args[0] if args else 3)
        if intent == "earnings":
            return self._fetch_earnings(symbol)
        candidates = self._ordered(intent)
        if not candidates:
            return unavailable_result("none", symbol, intent, "no configured provider supports this request")
        failures = []
        winner_index = -1
        primary = None
        for index, (name, provider) in enumerate(candidates):
            result = self._call(name, provider, intent, symbol, *args)
            if result.data is not None and result.status not in {DataStatus.UNAVAILABLE, DataStatus.ERROR}:
                if intent == "quote":
                    freshness = quote_freshness(
                        result.data_as_of, is_realtime=result.is_realtime,
                        is_delayed=result.is_delayed,
                        stale_after_seconds=settings.quote_stale_after_seconds,
                    )
                    result.freshness = freshness.value
                    result.market_status = market_session_status().value
                    if freshness == Freshness.STALE:
                        failures.append(f"{name}:stale")
                        continue
                if intent == "news" and isinstance(result.data, list):
                    result.data = filter_company_news(
                        [x for x in result.data if isinstance(x, NewsItem)], symbol,
                        args[0] if args else 5,
                    )
                    if not result.data:
                        failures.append(f"{name}:irrelevant")
                        continue
                primary, winner_index = result, index
                break
            failures.append(f"{name}:{result.error or result.status.value}")
            if index == 0:
                reliability_telemetry.increment("primary_provider_failed")
        if primary is None:
            return unavailable_result("none", symbol, intent, "; ".join(failures))
        if winner_index > 0:
            with self._lock: self.fallback_usage += 1
            reliability_telemetry.increment("fallback_provider_used")
        if verify and winner_index + 1 < len(candidates):
            for second_name, second_provider in candidates[winner_index + 1:]:
                secondary = self._call(second_name, second_provider, intent, symbol, *args)
                if secondary.data is not None:
                    primary = self._verify(primary, secondary, intent)
                    break
        return primary

    def _fetch_earnings(self, symbol: str) -> FinancialDataResult:
        """Return only a plausible next event with conservative verification metadata."""
        dated: list[FinancialDataResult] = []
        undated: list[FinancialDataResult] = []
        failures: list[str] = []
        today = current_utc_date()
        for name, provider in self._ordered("earnings"):
            result = self._call(name, provider, "earnings", symbol)
            event = result.data
            if not isinstance(event, EarningsData):
                failures.append(f"{name}:{result.error or result.status.value}")
                continue
            normalized = normalize_earnings_datetime(event.earnings_date or event.next_earnings_at)
            event.earnings_date = event.next_earnings_at = normalized
            event.source = event.source or result.source
            if normalized and normalized.date() > today:
                dated.append(result)
                if len(dated) == 2:
                    break
            else:
                event.status = "unverified"
                undated.append(result)
                source_label = (event.source or "").lower().replace("-", "_").replace(" ", "_")
                if source_label in {"investor_relations", "official_investor_relations", "official_ir"}:
                    result.status = DataStatus.PARTIAL
                    result.error = "official investor relations has not announced the next earnings date"
                    return result

        if not dated:
            if undated:
                event = undated[0].data
                event.status = "unverified"
                return undated[0]
            return unavailable_result("none", symbol, "earnings", "; ".join(failures) or "earnings date not announced")

        primary = dated[0]
        first: EarningsData = primary.data
        days_away = (first.earnings_date.date() - today).days
        if len(dated) == 1:
            if days_away > EARNINGS_MAX_UNVERIFIED_DAYS:
                first.status = "unverified"
                primary.status = DataStatus.PARTIAL
                primary.error = "distant earnings date lacks independent verification"
            elif first.status != "confirmed":
                first.status = "estimated"
            return primary

        secondary = dated[1]
        second: EarningsData = secondary.data
        difference = abs((first.earnings_date.date() - second.earnings_date.date()).days)
        first.verified_with = secondary.source
        primary.verification = {
            **(primary.verification or {}), "secondary_source": secondary.source,
            "earnings_date_difference_days": difference,
        }
        if difference > EARNINGS_AGREEMENT_DAYS:
            first.status = "unverified"
            primary.status = DataStatus.PARTIAL
            primary.error = "configured providers disagree on the next earnings date"
            primary.verification["disagreement"] = True
            with self._lock:
                self.conflicts += 1
            return primary

        if second.status == "confirmed" and first.status != "confirmed":
            first.earnings_date = first.next_earnings_at = second.earnings_date
            first.status = "confirmed"
            first.source = secondary.source
            first.verified_with = primary.source
            primary.source = secondary.source
        elif first.status != "confirmed":
            first.status = "estimated"
            if second.earnings_date < first.earnings_date:
                first.earnings_date = first.next_earnings_at = second.earnings_date
        return primary

    def _fetch_company_news(self, symbol: str, limit: int) -> FinancialDataResult:
        """Discover with NewsAPI, enrich with Finnhub, then use yfinance only as fallback."""
        collected: list[NewsItem] = []
        sources: list[str] = []
        failures: list[str] = []
        candidates = self._ordered("news")
        for name, provider in candidates:
            if name in {"rss", "yfinance"} and filter_company_news(collected, symbol, min(limit, 3)):
                break
            result = self._call(name, provider, "news", symbol, max(3, limit * 2))
            if isinstance(result.data, list):
                collected.extend(item for item in result.data if isinstance(item, NewsItem))
                sources.append(name)
            else:
                failures.append(f"{name}:{result.error or result.status.value}")
            # NewsAPI and Finnhub are intentionally combined; do not call a
            # third source when their normalized set is already sufficient.
            if name == "rss" and filter_company_news(collected, symbol, min(limit, 3)):
                break
        filtered = filter_company_news(collected, symbol, min(limit, 3))
        if not filtered:
            return unavailable_result("none", symbol, "news", "; ".join(failures) or "no sufficiently relevant news")
        now = dt.datetime.now(UTC)
        return FinancialDataResult(
            status=DataStatus.OK, source=" + ".join(sources), source_type="news",
            retrieved_at=now, data_as_of=max((item.published_at for item in filtered if item.published_at), default=None),
            symbol=symbol.upper(), data=filtered, is_realtime=False, is_delayed=True,
        )

    @staticmethod
    def _difference(left: float, right: float) -> float:
        return abs(left - right) / max(abs(left), abs(right), 1e-12) * 100

    def _verify(self, primary: FinancialDataResult, secondary: FinancialDataResult, intent: str) -> FinancialDataResult:
        checks = []
        if intent == "quote" and isinstance(primary.data, QuoteData) and isinstance(secondary.data, QuoteData):
            if primary.data.price is not None and secondary.data.price is not None:
                checks.append(("price", primary.data.price, secondary.data.price, settings.quote_verification_tolerance_pct))
            if primary.data.previous_close is not None and secondary.data.previous_close is not None:
                checks.append(("previous_close", primary.data.previous_close, secondary.data.previous_close, settings.quote_verification_tolerance_pct))
        elif intent == "fundamentals" and isinstance(primary.data, FundamentalsData) and isinstance(secondary.data, FundamentalsData):
            left, right = primary.data, secondary.data
            for field, tolerance in (("market_cap", settings.market_cap_verification_tolerance_pct), ("trailing_pe", settings.pe_verification_tolerance_pct)):
                left_def, right_def = left.metric_definitions.get(field), right.metric_definitions.get(field)
                same_definition = left_def == right_def and (bool(left_def) or field in {"trailing_pe", "market_cap"})
                same_currency = field != "market_cap" or not left.currency or not right.currency or left.currency == right.currency
                if same_definition and same_currency and getattr(left, field) is not None and getattr(right, field) is not None:
                    checks.append((field, getattr(left, field), getattr(right, field), tolerance))
            if left.revenue_period and left.revenue_period == right.revenue_period and (not left.currency or not right.currency or left.currency == right.currency) and left.revenue is not None and right.revenue is not None:
                checks.append(("revenue", left.revenue, right.revenue, 5.0))
            if left.margin_period and left.margin_period == right.margin_period and left.profit_margin is not None and right.profit_margin is not None:
                checks.append(("profit_margin", left.profit_margin, right.profit_margin, 10.0))
        conflicts = [(field, self._difference(a, b)) for field, a, b, tolerance in checks if self._difference(a, b) > tolerance]
        primary.verification = {
            **(primary.verification or {}), "secondary_source": secondary.source,
            "verified_fields": [field for field, *_ in checks],
            "differences_pct": {field: round(self._difference(a, b), 4) for field, a, b, _ in checks},
        }
        if conflicts:
            primary.status, primary.data = DataStatus.CONFLICTING_DATA, None
            primary.error = "Configured providers disagree on critical fields: " + ", ".join(field for field, _ in conflicts)
            primary.verification["disagreement"] = True
            with self._lock: self.conflicts += 1
        elif intent == "quote" and isinstance(primary.data, QuoteData) and isinstance(secondary.data, QuoteData):
            supplemented = []
            for field in ("previous_close", "change", "change_pct", "currency", "name"):
                if getattr(primary.data, field) is None and getattr(secondary.data, field) is not None:
                    setattr(primary.data, field, getattr(secondary.data, field))
                    supplemented.append(field)
            primary.verification["supplemented_fields"] = supplemented
        elif intent == "fundamentals" and isinstance(primary.data, FundamentalsData) and isinstance(secondary.data, FundamentalsData):
            supplemented = []
            for field in ("market_cap", "trailing_pe", "forward_pe", "fifty_two_week_high", "fifty_two_week_low"):
                equivalent = primary.data.metric_definitions.get(field) and primary.data.metric_definitions.get(field) == secondary.data.metric_definitions.get(field)
                if equivalent and getattr(primary.data, field) is None and getattr(secondary.data, field) is not None:
                    setattr(primary.data, field, getattr(secondary.data, field))
                    supplemented.append(field)
            for field, period_field in (("revenue", "revenue_period"), ("profit_margin", "margin_period")):
                if getattr(primary.data, field) is None and getattr(secondary.data, field) is not None:
                    setattr(primary.data, field, getattr(secondary.data, field))
                    setattr(primary.data, period_field, getattr(secondary.data, period_field))
                    supplemented.append(field)
            if primary.data.revenue_growth is None and secondary.data.revenue_growth is not None:
                primary.data.revenue_growth = secondary.data.revenue_growth
                supplemented.append("revenue_growth")
            primary.verification["supplemented_fields"] = supplemented
        return primary

    def health(self) -> dict:
        providers = {}
        now = time.monotonic()
        for name, state in self.health_state.items():
            cooldown_active = state.circuit_until > now and state.failure_reason != "invalid_credentials"
            providers[name] = {
                "provider": name, "configured": state.configured, "status": state.status,
                "last_status": state.status,
                "last_success_at": state.last_success_at.isoformat() if state.last_success_at else None,
                "last_failure_at": state.last_failure_at.isoformat() if state.last_failure_at else None,
                "last_success": state.last_success_at.isoformat() if state.last_success_at else None,
                "last_failure": state.last_failure_at.isoformat() if state.last_failure_at else None,
                "latency_ms": state.latency_ms, "failure_reason": state.failure_reason,
                "failure_category": state.failure_reason,
                "last_error_category": state.failure_reason,
                "cooldown_active": cooldown_active,
                "cooldown_until": state.cooldown_until.isoformat() if state.cooldown_until else None,
            }
        configured = [p for p in providers.values() if p["configured"]]
        overall = "ok" if any(p["status"] == "ok" for p in configured) else "degraded"
        return {"overall": overall, "providers": providers, "fallback_usage": self.fallback_usage, "conflicts": self.conflicts}
