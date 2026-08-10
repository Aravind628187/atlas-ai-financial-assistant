from __future__ import annotations

import datetime as dt
import asyncio
import os
import unittest
from unittest.mock import Mock, patch

import httpx

from app.services.financial_data_router import FinancialDataRouter
from app.services.news_relevance import filter_company_news
from app.services.news_relevance import is_verified_catalyst
from app.bot.formatting import reply_with_html_fallback, telegram_html
from app.services.providers.alpha_vantage_provider import AlphaVantageProvider
from app.services.providers.base import DataStatus, EarningsData, FinancialDataResult, FundamentalsData, NewsItem, QuoteData
from app.services.providers.finnhub_provider import FinnhubProvider
from app.services.providers.fmp_provider import FMPProvider
from app.services.providers.newsapi_provider import NewsAPIProvider
from app.services.providers.normalization import ratio
from app.services.providers.twelve_data_provider import TwelveDataProvider
from app.services.financial_data_gateway import TTLCache


UTC = dt.timezone.utc


def response(payload, status=200):
    value = Mock()
    value.json.return_value = payload
    value.raise_for_status.side_effect = None
    value.status_code = status
    return value


def result(data, source="provider"):
    now = dt.datetime.now(UTC)
    return FinancialDataResult(DataStatus.OK, source, "market_data", now, now, getattr(data, "symbol", "NVDA"), data, False, True)


class ProviderNormalizationTests(unittest.TestCase):
    @patch("app.services.providers.finnhub_provider.httpx.get")
    def test_finnhub_quote_normalization(self, get):
        get.return_value = response({"c": 120.5, "pc": 118, "d": 2.5, "dp": 2.1186, "t": 1700000000})
        value = FinnhubProvider("key").get_quote("NVDA")
        self.assertEqual(value.data.price, 120.5)
        self.assertEqual(value.data.previous_close, 118)
        self.assertEqual(value.data.change_pct, 2.1186)
        self.assertEqual(value.timestamp_kind, "quote")

    def test_finnhub_missing_key(self):
        self.assertEqual(FinnhubProvider("").get_quote("NVDA").status, DataStatus.NOT_CONFIGURED)

    def test_finnhub_auth_and_rate_limit_statuses(self):
        for code, expected in ((401, DataStatus.INVALID_CREDENTIALS), (429, DataStatus.RATE_LIMITED), (403, DataStatus.NOT_ENTITLED)):
            request = httpx.Request("GET", "https://finnhub.test/quote")
            failure = httpx.HTTPStatusError("failed", request=request, response=httpx.Response(code, request=request))
            client = Mock()
            client.get.return_value.raise_for_status.side_effect = failure
            self.assertEqual(FinnhubProvider("key", client).get_quote("NVDA").status, expected)

    @patch("app.services.providers.finnhub_provider.httpx.get")
    def test_finnhub_dividend_percent_is_normalized_once(self, get):
        get.return_value = response({"metric": {"dividendYieldIndicatedAnnual": 0.45}})
        value = FinnhubProvider("key").get_fundamentals("NVDA")
        self.assertAlmostEqual(value.data.dividend_yield, 0.0045)

    def test_fmp_fundamentals_normalization(self):
        client = Mock()
        client.get.side_effect = [
            response([{"marketCap": 2_000_000_000, "range": "90-140"}]),
            response([{"priceToEarningsRatioTTM": 25, "dividendYieldTTM": 0.0045}]),
            response([{"revenue": 1_000_000_000, "netIncome": 200_000_000, "period": "FY"}]),
        ]
        data = FMPProvider("key", client).get_fundamentals("NVDA").data
        self.assertEqual(data.market_cap, 2_000_000_000)
        self.assertEqual(data.revenue, 1_000_000_000)
        self.assertAlmostEqual(data.profit_margin, 0.20)
        self.assertAlmostEqual(data.dividend_yield, 0.0045)

    def test_fmp_earnings_selects_earliest_future_record(self):
        today = dt.date.today()
        client = Mock()
        client.get.return_value = response([
            {"date": str(today + dt.timedelta(days=95)), "fiscalDateEnding": "later"},
            {"date": str(today - dt.timedelta(days=2)), "fiscalDateEnding": "past"},
            {"date": str(today + dt.timedelta(days=28)), "fiscalDateEnding": "next"},
        ])
        event = FMPProvider("key", client).get_earnings("MSFT").data
        self.assertEqual(event.earnings_date.date(), today + dt.timedelta(days=28))
        self.assertEqual(event.period, "next")
        self.assertEqual(event.status, "estimated")

    def test_finnhub_earnings_multiple_rows_select_earliest_future(self):
        today = dt.date.today()
        client = Mock()
        client.get.return_value = response({"earningsCalendar": [
            {"date": str(today + dt.timedelta(days=80)), "quarter": 4},
            {"date": str(today + dt.timedelta(days=20)), "quarter": 3},
        ]})
        event = FinnhubProvider("key", client).get_earnings("MSFT").data
        self.assertEqual(event.earnings_date.date(), today + dt.timedelta(days=20))

    def test_twelve_data_quote_normalization(self):
        client = Mock()
        client.get.return_value = response({
            "symbol": "NVDA", "close": "180.50", "previous_close": "178.00",
            "change": "2.50", "percent_change": "1.4045", "currency": "USD", "timestamp": 1700000000,
        })
        value = TwelveDataProvider("key", client).get_quote("NVDA")
        data = value.data
        self.assertEqual(data.price, 180.5)
        self.assertEqual(data.change_pct, 1.4045)
        self.assertEqual(value.timestamp_kind, "quote")

    def test_twelve_data_daily_bar_is_a_session_date_not_quote_time(self):
        client = Mock()
        client.get.return_value = response({
            "meta": {"exchange_timezone": "America/New_York"},
            "values": [{"datetime": "2026-08-07", "open": "220", "high": "225", "low": "219", "close": "223.96", "volume": "10"}],
        })
        value = TwelveDataProvider("key", client).get_history("NVDA", "1mo")
        self.assertEqual(value.timestamp_kind, "daily_bar_date")
        self.assertEqual(value.data_date, dt.date(2026, 8, 7))
        self.assertIsNone(value.data_as_of)
        self.assertEqual(value.exchange_timezone, "America/New_York")

    def test_alpha_vantage_fundamentals_normalization(self):
        client = Mock()
        client.get.return_value = response({
            "Symbol": "NVDA", "MarketCapitalization": "2000000000", "PERatio": "30",
            "RevenueTTM": "1000000000", "ProfitMargin": "0.25",
            "QuarterlyRevenueGrowthYOY": "0.18", "DividendYield": "0.0045",
        })
        data = AlphaVantageProvider("key", client).get_fundamentals("NVDA").data
        self.assertEqual(data.trailing_pe, 30)
        self.assertEqual(data.profit_margin, 0.25)
        self.assertEqual(data.revenue_growth, 0.18)
        self.assertEqual(data.dividend_yield, 0.0045)

    def test_newsapi_parsing(self):
        client = Mock()
        client.get.return_value = response({"status": "ok", "articles": [{
            "title": "Nvidia launches a new data-center platform", "url": "https://example.com/nvda",
            "publishedAt": "2026-08-10T10:00:00Z", "description": "Nvidia announced the platform.",
            "source": {"name": "Example Wire"},
        }]})
        value = NewsAPIProvider("key", client).get_news("NVDA", 3)
        self.assertEqual(value.data[0].publisher, "Example Wire")
        self.assertIsNotNone(value.data[0].published_at)

    def test_missing_key_is_not_configured(self):
        self.assertEqual(FMPProvider("").get_quote("NVDA").status, DataStatus.NOT_CONFIGURED)
        self.assertEqual(TwelveDataProvider("").get_quote("NVDA").status, DataStatus.NOT_CONFIGURED)
        self.assertEqual(AlphaVantageProvider("").get_fundamentals("NVDA").status, DataStatus.NOT_CONFIGURED)

    def test_dividend_yield_provider_semantics(self):
        self.assertEqual(ratio(0.0045, unit="fraction", minimum=0, maximum=1), 0.0045)
        self.assertAlmostEqual(ratio(0.45, unit="percent", minimum=0, maximum=1), 0.0045)
        self.assertIsNone(ratio(45, unit="fraction", minimum=0, maximum=1))

    def test_cached_delayed_result_is_not_relabeled_live(self):
        cache = TTLCache()
        value = result(QuoteData("NVDA", 100), "finnhub")
        value.is_realtime, value.is_delayed = False, True
        cache.set("quote", value, 30)
        cached = cache.get("quote")
        self.assertTrue(cached.cache_hit)
        self.assertFalse(cached.is_realtime)
        self.assertTrue(cached.is_delayed)


class FakeProvider:
    configured = True

    def __init__(self, name, quote=None, fundamentals=None, earnings=None, error=None):
        self.name, self.quote, self.fundamentals, self.earnings, self.error = name, quote, fundamentals, earnings, error
        self.calls = 0

    def get_quote(self, symbol):
        self.calls += 1
        if self.error: raise self.error
        return result(self.quote, self.name) if self.quote else FinancialDataResult(
            DataStatus.UNAVAILABLE, self.name, "quote", dt.datetime.now(UTC), None, symbol, None, error="unavailable"
        )

    def get_fundamentals(self, symbol):
        self.calls += 1
        if self.error: raise self.error
        return result(self.fundamentals, self.name) if self.fundamentals else FinancialDataResult(
            DataStatus.UNAVAILABLE, self.name, "fundamentals", dt.datetime.now(UTC), None, symbol, None, error="unavailable"
        )

    def get_earnings(self, symbol):
        self.calls += 1
        if self.error: raise self.error
        return result(self.earnings, self.name) if self.earnings else FinancialDataResult(
            DataStatus.UNAVAILABLE, self.name, "earnings", dt.datetime.now(UTC), None, symbol, None, error="unavailable"
        )


class RouterReliabilityTests(unittest.TestCase):
    def test_earnings_provider_order_is_strict(self):
        self.assertEqual(FinancialDataRouter.ROUTES["earnings"], ["fmp", "finnhub", "alpha_vantage"])

    def test_primary_failure_secondary_success(self):
        primary = FakeProvider("finnhub", error=TimeoutError("timeout"))
        secondary = FakeProvider("twelve_data", quote=QuoteData("NVDA", 100, 99, 1, 1.01))
        router = FinancialDataRouter({"finnhub": primary, "twelve_data": secondary})
        with patch("app.services.financial_data_router.settings.financial_provider_max_retries", 1), patch(
            "app.services.financial_data_router.settings.market_data_provider", "finnhub"
        ):
            value = router.fetch("quote", "NVDA")
        self.assertEqual(value.source, "twelve_data")
        self.assertEqual(router.fallback_usage, 1)

    def test_all_providers_fail_without_data(self):
        router = FinancialDataRouter({"finnhub": FakeProvider("finnhub", error=TimeoutError())})
        with patch("app.services.financial_data_router.settings.financial_provider_max_retries", 1):
            value = router.fetch("quote", "NVDA")
        self.assertEqual(value.status, DataStatus.UNAVAILABLE)
        self.assertIsNone(value.data)

    def test_quote_agreement(self):
        first = FakeProvider("finnhub", quote=QuoteData("NVDA", 100, 99))
        second = FakeProvider("twelve_data", quote=QuoteData("NVDA", 100.1, 99))
        router = FinancialDataRouter({"finnhub": first, "twelve_data": second})
        with patch("app.services.financial_data_router.settings.market_data_provider", "finnhub"):
            value = router.fetch("quote", "NVDA", verify=True)
        self.assertEqual(value.status, DataStatus.OK)
        self.assertEqual(value.verification["secondary_source"], "twelve_data")

    def test_quote_disagreement_fails_closed(self):
        first = FakeProvider("finnhub", quote=QuoteData("NVDA", 100, 99))
        second = FakeProvider("twelve_data", quote=QuoteData("NVDA", 110, 99))
        router = FinancialDataRouter({"finnhub": first, "twelve_data": second})
        with patch("app.services.financial_data_router.settings.market_data_provider", "finnhub"):
            value = router.fetch("quote", "NVDA", verify=True)
        self.assertEqual(value.status, DataStatus.CONFLICTING_DATA)
        self.assertIsNone(value.data)

    def test_quote_verifier_falls_back_only_when_twelve_data_fails(self):
        first = FakeProvider("finnhub", quote=QuoteData("NVDA", 100, 99))
        second = FakeProvider("twelve_data", error=TimeoutError("down"))
        third = FakeProvider("yfinance", quote=QuoteData("NVDA", 100.1, 99))
        router = FinancialDataRouter({"finnhub": first, "twelve_data": second, "yfinance": third})
        with patch("app.services.financial_data_router.settings.financial_provider_max_retries", 1):
            value = router.fetch("quote", "NVDA", verify=True)
        self.assertEqual(value.verification["secondary_source"], "yfinance")
        self.assertEqual(first.calls, 1)
        self.assertEqual(second.calls, 1)
        self.assertEqual(third.calls, 1)

    def test_pe_disagreement_fails_closed(self):
        first = FakeProvider("fmp", fundamentals=FundamentalsData("NVDA", trailing_pe=20))
        second = FakeProvider("finnhub", fundamentals=FundamentalsData("NVDA", trailing_pe=40))
        router = FinancialDataRouter({"fmp": first, "finnhub": second})
        value = router.fetch("fundamentals", "NVDA", verify=True)
        self.assertEqual(value.status, DataStatus.CONFLICTING_DATA)

    def test_incompatible_revenue_periods_are_not_compared(self):
        first = FakeProvider("fmp", fundamentals=FundamentalsData("NVDA", revenue=100, revenue_period="annual"))
        second = FakeProvider("finnhub", fundamentals=FundamentalsData("NVDA", revenue=25, revenue_period="quarterly"))
        router = FinancialDataRouter({"fmp": first, "finnhub": second})
        value = router.fetch("fundamentals", "NVDA", verify=True)
        self.assertEqual(value.status, DataStatus.OK)
        self.assertNotIn("revenue", value.verification["verified_fields"])

    def test_429_marks_provider_rate_limited(self):
        request = httpx.Request("GET", "https://provider.test")
        error = httpx.HTTPStatusError("rate", request=request, response=httpx.Response(429, request=request))
        provider = FakeProvider("finnhub", error=error)
        router = FinancialDataRouter({"finnhub": provider})
        router.fetch("quote", "NVDA")
        self.assertEqual(router.health()["providers"]["finnhub"]["status"], "rate_limited")

    def test_auth_failure_opens_circuit_without_repeat_call(self):
        request = httpx.Request("GET", "https://provider.test")
        error = httpx.HTTPStatusError("auth", request=request, response=httpx.Response(401, request=request))
        provider = FakeProvider("finnhub", error=error)
        router = FinancialDataRouter({"finnhub": provider})
        router.fetch("quote", "NVDA")
        router.fetch("quote", "NVDA")
        self.assertEqual(provider.calls, 1)
        self.assertEqual(router.health()["providers"]["finnhub"]["status"], "invalid_credentials")

    def test_distant_earnings_date_without_verifier_is_unverified(self):
        today = dt.date(2026, 8, 10)
        distant = dt.datetime(2027, 2, 2, tzinfo=UTC)
        router = FinancialDataRouter({"fmp": FakeProvider("fmp", earnings=EarningsData("GOOGL", distant, status="estimated", source="fmp"))})
        with patch("app.services.financial_data_router.current_utc_date", return_value=today):
            value = router.fetch("earnings", "GOOGL")
        self.assertEqual(value.data.status, "unverified")
        self.assertEqual(value.status, DataStatus.PARTIAL)

    def test_earnings_provider_disagreement_is_unverified(self):
        today = dt.date(2026, 8, 10)
        fmp = EarningsData("MSFT", dt.datetime(2026, 10, 20, tzinfo=UTC), status="estimated", source="fmp")
        finnhub = EarningsData("MSFT", dt.datetime(2026, 11, 5, tzinfo=UTC), status="estimated", source="finnhub")
        router = FinancialDataRouter({"fmp": FakeProvider("fmp", earnings=fmp), "finnhub": FakeProvider("finnhub", earnings=finnhub)})
        with patch("app.services.financial_data_router.current_utc_date", return_value=today):
            value = router.fetch("earnings", "MSFT")
        self.assertEqual(value.data.status, "unverified")
        self.assertTrue(value.verification["disagreement"])

    def test_no_announced_earnings_date_remains_unverified(self):
        event = EarningsData("MSFT", None, status="unverified", source="investor_relations")
        estimate = EarningsData("MSFT", dt.datetime.now(UTC) + dt.timedelta(days=45), status="estimated", source="finnhub")
        secondary = FakeProvider("finnhub", earnings=estimate)
        router = FinancialDataRouter({"fmp": FakeProvider("fmp", earnings=event), "finnhub": secondary})
        value = router.fetch("earnings", "MSFT")
        self.assertEqual(value.data.status, "unverified")
        self.assertIsNone(value.data.earnings_date)
        self.assertEqual(secondary.calls, 0)

    def test_confirmed_earnings_date_is_preserved_and_verified(self):
        today = dt.date(2026, 8, 10)
        date = dt.datetime(2026, 10, 26, tzinfo=UTC)
        fmp = EarningsData("MSFT", date, status="confirmed", source="fmp")
        finnhub = EarningsData("MSFT", date, status="estimated", source="finnhub")
        router = FinancialDataRouter({"fmp": FakeProvider("fmp", earnings=fmp), "finnhub": FakeProvider("finnhub", earnings=finnhub)})
        with patch("app.services.financial_data_router.current_utc_date", return_value=today):
            value = router.fetch("earnings", "MSFT")
        self.assertEqual(value.data.status, "confirmed")
        self.assertEqual(value.data.verified_with, "finnhub")

    def test_plausible_single_provider_date_is_estimated(self):
        today = dt.date(2026, 8, 10)
        date = dt.datetime(2026, 10, 26, tzinfo=UTC)
        event = EarningsData("MSFT", date, status="estimated", source="fmp")
        router = FinancialDataRouter({"fmp": FakeProvider("fmp", earnings=event)})
        with patch("app.services.financial_data_router.current_utc_date", return_value=today):
            value = router.fetch("earnings", "MSFT")
        self.assertEqual(value.data.status, "estimated")

    def test_incompatible_pe_definitions_are_not_compared(self):
        first = FakeProvider("fmp", fundamentals=FundamentalsData("NVDA", trailing_pe=20, metric_definitions={"trailing_pe": "trailing"}))
        second = FakeProvider("alpha_vantage", fundamentals=FundamentalsData("NVDA", trailing_pe=40, metric_definitions={"trailing_pe": "forward"}))
        router = FinancialDataRouter({"fmp": first, "alpha_vantage": second})
        value = router.fetch("fundamentals", "NVDA", verify=True)
        self.assertEqual(value.status, DataStatus.OK)
        self.assertNotIn("trailing_pe", value.verification["verified_fields"])


class NewsQualityTests(unittest.TestCase):
    def test_news_relevance_and_deduplication(self):
        now = dt.datetime.now(UTC)
        items = [
            NewsItem("1", "Nvidia launches new AI platform", "Wire", now, "https://a.com/story?utm=x", "Nvidia announced it."),
            NewsItem("2", "NVIDIA launches a new AI platform", "Other", now, "https://b.com/copy", "Nvidia announced it."),
            NewsItem("3", "Micron shares rise after earnings", "Wire", now, "https://a.com/mu", "Memory market update"),
            NewsItem("4", "Bitcoin and crypto ETF weekly roundup", "Blog", now, "https://a.com/crypto", "No company catalyst"),
        ]
        filtered = filter_company_news(items, "NVDA", 5)
        self.assertEqual(len(filtered), 1)
        self.assertIn("Nvidia", filtered[0].headline)

    def test_competitor_etf_and_crypto_headlines_are_rejected(self):
        now = dt.datetime.now(UTC)
        rejected = [
            NewsItem("1", "AMD unveils its newest accelerator", "Wire", now, summary="It competes with Nvidia."),
            NewsItem("2", "Micron earnings beat estimates", "Wire", now, summary="Nvidia is a customer."),
            NewsItem("3", "Nvidia ETF roundup for investors", "Blog", now),
            NewsItem("4", "Bitcoin and crypto rally", "Blog", now, summary="Nvidia was mentioned."),
        ]
        self.assertEqual(filter_company_news(rejected, "NVDA", 3), [])

    def test_catalyst_requires_explicit_move_link(self):
        now = dt.datetime.now(UTC)
        related = NewsItem("1", "Nvidia announces new platform", "Wire", now)
        catalyst = NewsItem("2", "Nvidia shares rise after new platform launch", "Wire", now)
        self.assertFalse(is_verified_catalyst(related, "NVDA"))
        self.assertTrue(is_verified_catalyst(catalyst, "NVDA"))


class TelegramFormattingTests(unittest.TestCase):
    def test_html_is_escaped_before_formatting(self):
        rendered = telegram_html("**NVDA < latest & safe**")
        self.assertEqual(rendered, "<b>NVDA &lt; latest &amp; safe</b>")

    def test_html_failure_falls_back_to_plain_text(self):
        reply = Mock()
        calls = []

        async def sender(text, **kwargs):
            calls.append((text, kwargs))
            if len(calls) == 1:
                raise RuntimeError("parse failed")

        asyncio.run(reply_with_html_fallback(sender, "**NVDA** & AMD"))
        self.assertEqual(calls[0][1]["parse_mode"], "HTML")
        self.assertEqual(calls[1][0], "NVDA & AMD")


@unittest.skipUnless(os.getenv("RUN_LIVE_FINANCIAL_TESTS") == "1", "live provider tests are opt-in")
class OptionalLiveProviderTests(unittest.TestCase):
    def test_live_router_health(self):
        router = FinancialDataRouter()
        value = router.fetch("quote", "NVDA")
        self.assertIn(value.status, {DataStatus.OK, DataStatus.DELAYED, DataStatus.UNAVAILABLE, DataStatus.STALE})


if __name__ == "__main__":
    unittest.main()
