from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import Mock, patch

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.brain import _news_reply, _quote_reply, handle_text_turn
from app.ai.llm_gateway import LLMGateway, SecondaryLLMUnavailableError
from app.database import Base
from app.models import Alert, OnboardingStage, User, WatchlistItem
from app.services.financial_data_gateway import FinancialDataGateway, TTLCache
from app.services.financial_data_router import FinancialDataRouter
from app.services.providers.base import (
    DataStatus, FilingItem, FilingsData, FinancialDataResult, FundamentalsData, NewsItem, QuoteData,
)


UTC = dt.timezone.utc


def quote_result(price: float = 100.0, source: str = "provider") -> FinancialDataResult:
    now = dt.datetime.now(UTC)
    return FinancialDataResult(
        status=DataStatus.OK,
        source=source,
        source_type="quote",
        retrieved_at=now,
        data_as_of=now,
        symbol="NVDA",
        data=QuoteData("NVDA", price, price - 1, 1, 1.01, "USD"),
        is_realtime=False,
        is_delayed=True,
        verification={"freshness": "delayed", "market_session": "closed"},
    )


class QuoteProvider:
    configured = True

    def __init__(self, name: str, value: FinancialDataResult | None = None,
                 error: Exception | None = None) -> None:
        self.name = name
        self.value = value
        self.error = error
        self.calls = 0

    def get_quote(self, symbol: str) -> FinancialDataResult:
        self.calls += 1
        if self.error:
            raise self.error
        if self.value:
            return self.value
        return FinancialDataResult(
            DataStatus.UNAVAILABLE, self.name, "quote", dt.datetime.now(UTC),
            None, symbol, None, error="temporarily unavailable",
        )


def http_failure(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://provider.test/quote")
    return httpx.HTTPStatusError(
        "upstream request failed", request=request,
        response=httpx.Response(status, request=request),
    )


class FinancialFailoverTests(unittest.TestCase):
    def test_finnhub_429_falls_back_to_twelve_data(self):
        finnhub = QuoteProvider("finnhub", error=http_failure(429))
        twelve = QuoteProvider("twelve_data", quote_result(101, "twelve_data"))
        router = FinancialDataRouter({"finnhub": finnhub, "twelve_data": twelve})
        with patch("app.services.financial_data_router.settings.financial_provider_max_retries", 1):
            value = router.fetch("quote", "NVDA")
        self.assertEqual(value.source, "twelve_data")
        self.assertEqual(value.data.price, 101)
        self.assertEqual(finnhub.calls, 1)
        self.assertEqual(twelve.calls, 1)

    def test_finnhub_and_twelve_fail_then_yfinance_succeeds(self):
        finnhub = QuoteProvider("finnhub", error=http_failure(429))
        twelve = QuoteProvider("twelve_data", error=TimeoutError("timeout"))
        yahoo = QuoteProvider("yfinance", quote_result(102, "yfinance"))
        router = FinancialDataRouter({
            "finnhub": finnhub, "twelve_data": twelve, "yfinance": yahoo,
        })
        with patch("app.services.financial_data_router.settings.financial_provider_max_retries", 1):
            value = router.fetch("quote", "NVDA")
        self.assertEqual(value.source, "yfinance")
        self.assertEqual(value.data.price, 102)

    def test_rate_limit_circuit_skips_calls_and_recovers_after_cooldown(self):
        provider = QuoteProvider("finnhub", error=http_failure(429))
        router = FinancialDataRouter({"finnhub": provider})
        with patch("app.services.financial_data_router.settings.financial_provider_max_retries", 1), patch(
            "app.services.financial_data_router.time.monotonic", return_value=100.0,
        ):
            router.fetch("quote", "NVDA")
        provider.error = None
        provider.value = quote_result(103, "finnhub")
        with patch("app.services.financial_data_router.time.monotonic", return_value=200.0):
            blocked = router.fetch("quote", "NVDA")
        self.assertEqual(blocked.status, DataStatus.UNAVAILABLE)
        self.assertEqual(provider.calls, 1)
        with patch("app.services.financial_data_router.time.monotonic", return_value=1901.0):
            recovered = router.fetch("quote", "NVDA")
        self.assertEqual(recovered.data.price, 103)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(router.health()["providers"]["finnhub"]["status"], "ok")

    def test_expired_success_is_available_only_as_labeled_last_verified_data(self):
        cache = TTLCache()
        cached_value = quote_result(104, "finnhub")
        with patch("app.services.financial_data_gateway.time.monotonic", return_value=10.0):
            cache.set("quote", cached_value, 1)
        with patch("app.services.financial_data_gateway.time.monotonic", return_value=12.0):
            self.assertIsNone(cache.get("quote"))
            fallback = cache.get_last_verified("quote", 60)
        self.assertEqual(fallback.status, DataStatus.STALE)
        self.assertEqual(fallback.data.price, 104)
        self.assertTrue(fallback.verification["cached_verified"])

        with patch("app.ai.brain.gateway.get_quote", return_value=fallback):
            reply = _quote_reply("NVDA")
        self.assertIn("Last Verified Data", reply)
        self.assertIn("cached verified value, not a live quote", reply)

    def test_all_current_quote_providers_fail_returns_cached_verified_quote(self):
        provider = QuoteProvider("yfinance", value=quote_result(104, "yfinance"))
        gateway = FinancialDataGateway(primary=provider)
        gateway.secondary = None
        with patch.object(gateway, "_log_fetch"), patch(
            "app.services.financial_data_gateway.settings.quote_cache_ttl_seconds", 0,
        ), patch("app.services.financial_data_gateway.settings.provider_max_retries", 1):
            initial = gateway.get_quote("NVDA")
            provider.value = None
            provider.error = TimeoutError("current providers unavailable")
            fallback = gateway.get_quote("NVDA")
        self.assertEqual(initial.data.price, 104)
        self.assertEqual(fallback.status, DataStatus.STALE)
        self.assertEqual(fallback.data.price, 104)
        self.assertTrue(fallback.verification["cached_verified"])

    def test_all_providers_fail_without_cache_returns_no_value(self):
        provider = QuoteProvider("yfinance", error=TimeoutError("timeout"))
        gateway = FinancialDataGateway(primary=provider)
        gateway.secondary = None
        with patch.object(gateway, "_log_fetch"), patch(
            "app.services.financial_data_gateway.settings.provider_max_retries", 1,
        ):
            value = gateway.get_quote("NVDA")
        self.assertEqual(value.status, DataStatus.UNAVAILABLE)
        self.assertIsNone(value.data)

    def test_raw_provider_failure_is_not_exposed_to_user(self):
        unavailable = FinancialDataResult(
            DataStatus.UNAVAILABLE, "none", "quote", dt.datetime.now(UTC),
            None, "NVDA", None, error="429 quota exceeded secret-key-value",
        )
        with patch("app.ai.brain.gateway.get_quote", return_value=unavailable):
            reply = _quote_reply("NVDA")
        self.assertNotIn("429", reply)
        self.assertNotIn("quota", reply.lower())
        self.assertNotIn("secret-key-value", reply)
        self.assertIn("can't verify", reply)


class LLMFailoverTests(unittest.TestCase):
    def test_primary_failure_uses_optional_secondary(self):
        primary = Mock()
        primary.generate.side_effect = RuntimeError("429 quota")
        secondary = Mock()
        secondary.generate.return_value = "Safe secondary response"
        gateway = LLMGateway(primary=primary, secondary=secondary)
        self.assertEqual(gateway.generate("hello"), "Safe secondary response")
        secondary.generate.assert_called_once()

    @patch("app.ai.llm_gateway.configured_secondary", return_value=None)
    def test_no_secondary_signals_deterministic_fallback_without_raw_error(self, _configured):
        primary = Mock()
        primary.generate.side_effect = RuntimeError("429 quota secret-key-value")
        gateway = LLMGateway(primary=primary)
        with self.assertRaisesRegex(SecondaryLLMUnavailableError, "No text synthesis provider") as raised:
            gateway.generate("hello")
        message = str(raised.exception)
        self.assertNotIn("429", message)
        self.assertNotIn("quota", message.lower())
        self.assertNotIn("secret-key-value", message)


class DeterministicFeatureOutageTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(telegram_id=7001, onboarding_stage=OnboardingStage.DONE.value)
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_core_features_remain_useful_when_all_text_llms_are_unavailable(self):
        filing = FilingItem(
            "AAPL", "Apple Inc.", "10-Q", dt.date(2026, 8, 1),
            url="https://www.sec.gov/example",
        )
        with patch("app.ai.llm_gateway.llm_gateway.generate", side_effect=SecondaryLLMUnavailableError()), patch(
            "app.ai.llm_gateway.llm_gateway.generate_json", side_effect=SecondaryLLMUnavailableError(),
        ) as generate_json, patch(
            "app.ai.brain.gateway.get_quote", return_value=quote_result(105, "finnhub"),
        ), patch(
            "app.ai.brain.gateway.get_fundamentals",
            side_effect=lambda symbol: FinancialDataResult(
                DataStatus.OK, "fmp", "fundamentals", dt.datetime.now(UTC), dt.datetime.now(UTC),
                symbol, FundamentalsData(symbol, trailing_pe=30 if symbol == "NVDA" else 20),
            ),
        ), patch(
            "app.ai.brain.gateway.get_filings",
            return_value=FinancialDataResult(
                DataStatus.OK, "SEC EDGAR", "filings", dt.datetime.now(UTC), None,
                "AAPL", FilingsData("AAPL", "0000320193", "Apple Inc.", [filing]),
            ),
        ), patch(
            "app.services.briefing_service.gateway.get_quote", return_value=quote_result(105, "finnhub"),
        ), patch(
            "app.services.briefing_service.gateway.get_news",
            return_value=FinancialDataResult(
                DataStatus.UNAVAILABLE, "none", "news", dt.datetime.now(UTC), None, "NVDA", None,
            ),
        ), patch(
            "app.services.briefing_service.gateway.get_earnings",
            return_value=FinancialDataResult(
                DataStatus.UNAVAILABLE, "none", "earnings", dt.datetime.now(UTC), None, "NVDA", None,
            ),
        ):
            quote = handle_text_turn(self.db, self.user, "What is Nvidia's price?")
            comparison = handle_text_turn(self.db, self.user, "Compare Nvidia and AMD.")

            self.db.add(WatchlistItem(user_id=self.user.id, symbol="NVDA"))
            self.db.add(Alert(user_id=self.user.id, symbol="NVDA", threshold_pct=5, active=True))
            self.db.flush()
            watchlist = handle_text_turn(self.db, self.user, "Show my watchlist.")
            alerts = handle_text_turn(self.db, self.user, "Show my alerts.")
            briefing = handle_text_turn(self.db, self.user, "Give me my morning briefing.")
            filing_reply = handle_text_turn(self.db, self.user, "Show Apple's latest 10-Q.")

            newcomer = User(
                telegram_id=7002, onboarding_stage=OnboardingStage.ASKED_ROLE.value,
            )
            self.db.add(newcomer)
            self.db.flush()
            onboarding = handle_text_turn(self.db, newcomer, "student")

        self.assertIn("105.00", quote)
        self.assertIn("NVDA vs AMD", comparison)
        self.assertIn("NVDA", watchlist)
        self.assertIn("5%", alerts)
        self.assertIn("WATCHLIST SNAPSHOT", briefing)
        self.assertIn("SEC EDGAR", filing_reply)
        self.assertIn("Which companies", onboarding)
        self.assertNotIn("rate-limited", "\n".join(
            (quote, comparison, watchlist, alerts, briefing, filing_reply, onboarding),
        ).lower())
        generate_json.assert_not_called()

    def test_news_failure_preserves_verified_quote(self):
        unavailable_news = FinancialDataResult(
            DataStatus.UNAVAILABLE, "none", "news", dt.datetime.now(UTC),
            None, "NVDA", None, error="429 quota exceeded",
        )
        with patch("app.ai.brain.gateway.get_quote", return_value=quote_result(106, "finnhub")), patch(
            "app.ai.brain.gateway.get_news", return_value=unavailable_news,
        ):
            reply = _news_reply("NVDA")
        self.assertIn("+1.01%", reply)
        self.assertIn("couldn't verify a specific recent catalyst", reply)
        self.assertNotIn("429", reply)

    def test_fundamentals_failure_does_not_discard_quote_and_news(self):
        unavailable_fundamentals = FinancialDataResult(
            DataStatus.UNAVAILABLE, "none", "fundamentals", dt.datetime.now(UTC),
            None, "NVDA", None, error="provider quota exceeded",
        )
        headline = NewsItem(
            "n1", "Nvidia publishes a product update", "Example Wire", dt.datetime.now(UTC),
        )
        news = FinancialDataResult(
            DataStatus.OK, "newsapi", "news", dt.datetime.now(UTC), dt.datetime.now(UTC),
            "NVDA", [headline],
        )
        with patch("app.ai.brain.gateway.get_quote", return_value=quote_result(107, "finnhub")), patch(
            "app.ai.brain.gateway.get_fundamentals", return_value=unavailable_fundamentals,
        ), patch("app.ai.brain.gateway.get_news", return_value=news):
            reply = handle_text_turn(
                self.db, self.user, "Give me Nvidia price, fundamentals and latest news.",
            )
        self.assertIn("107.00", reply)
        self.assertIn("can't verify the requested fundamentals", reply)
        self.assertIn("Nvidia publishes a product update", reply)
        self.assertNotIn("quota", reply.lower())


if __name__ == "__main__":
    unittest.main()
