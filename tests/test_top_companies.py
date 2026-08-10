from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai.brain import handle_text_turn
from app.ai.intent_router import route
from app.database import Base
from app.models import Alert, OnboardingStage, User, WatchlistItem
from app.services.briefing_service import build_morning_brief
from app.services.providers.base import DataStatus, FinancialDataResult, QuoteData
from app.services.top_companies_service import (
    FALLBACK_SEED, TopCompaniesRanking, TopCompaniesService, TopCompany,
)

UTC = dt.timezone.utc


def financial_result(data=None, status=DataStatus.OK, source="test"):
    now = dt.datetime.now(UTC)
    return FinancialDataResult(status, source, "market_data", now, now,
                               getattr(data, "symbol", None), data, False, True)


def ranking(companies, source="fmp"):
    return TopCompaniesRanking(tuple(companies), source, dt.datetime(2026, 8, 10, 8, 0, tzinfo=UTC), source == "fmp")


class TopCompaniesServiceTests(unittest.TestCase):
    def test_fmp_results_are_sorted_and_share_classes_deduplicated(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {"symbol": "MSFT", "companyName": "Microsoft", "marketCap": 300, "exchangeShortName": "NASDAQ"},
            {"symbol": "GOOG", "companyName": "Alphabet", "marketCap": 500, "exchangeShortName": "NASDAQ"},
            {"symbol": "GOOGL", "companyName": "Alphabet", "marketCap": 500, "exchangeShortName": "NASDAQ"},
            {"symbol": "AAPL", "companyName": "Apple", "marketCap": 400, "exchangeShortName": "NASDAQ"},
        ]
        client = Mock()
        client.get.return_value = response
        service = TopCompaniesService(client=client)
        with patch("app.services.top_companies_service.settings.fmp_api_key", "configured"):
            value = service.get_top(5)
        self.assertEqual([item.symbol for item in value.companies], ["GOOGL", "AAPL", "MSFT"])
        self.assertEqual(value.source, "fmp")

    def test_fmp_failure_uses_labeled_fallback_seed(self):
        client = Mock()
        client.get.side_effect = RuntimeError("not entitled")
        service = TopCompaniesService(client=client)
        with patch("app.services.top_companies_service.settings.fmp_api_key", "configured"):
            value = service.get_top(15)
        self.assertEqual(value.source, "fallback_seed")
        self.assertFalse(value.is_live)
        self.assertEqual(value.companies[0].symbol, "NVDA")

    def test_ranking_cache_avoids_repeated_provider_calls(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = [
            {"symbol": f"T{i}", "companyName": f"Company {i}", "marketCap": 1000 - i,
             "exchangeShortName": "NYSE"} for i in range(20)
        ]
        client = Mock()
        client.get.return_value = response
        service = TopCompaniesService(client=client)
        with patch("app.services.top_companies_service.settings.fmp_api_key", "configured"):
            service.get_top(15)
            service.get_top(10)
        self.assertEqual(client.get.call_count, 1)


class TopCompaniesConversationTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(telegram_id=800, onboarding_stage=OnboardingStage.DONE.value)
        self.db.add(self.user)
        self.db.flush()
        self.companies = list(FALLBACK_SEED[:20])

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def valid_quote(symbol, verify=False):
        if symbol == "SPCX":
            return financial_result(None, DataStatus.UNAVAILABLE)
        return financial_result(QuoteData(symbol, 100, 99, 1, 1.01, "USD"))

    def test_top_company_requests_route_without_gemini(self):
        cases = {
            "Track the top 15 companies.": "top_companies_add",
            "Show me the top 15 companies.": "top_companies_show",
            "Track the biggest 10 companies.": "top_companies_add",
            "Remove the top 15 companies from my watchlist.": "top_companies_remove",
        }
        for phrase, expected in cases.items():
            routed = route(phrase)
            self.assertEqual(routed.intent, expected)
            self.assertFalse(routed.ai_called)

    def test_more_than_25_requests_smaller_watchlist(self):
        reply = handle_text_turn(self.db, self.user, "Track the top 30 companies.")
        self.assertIn("25 companies or fewer", reply)
        self.assertEqual(self.db.scalars(select(WatchlistItem)).all(), [])

    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_show_top_15_does_not_modify_watchlist(self, get_top, get_quote):
        get_top.return_value = ranking(self.companies, "fallback_seed")
        get_quote.side_effect = self.valid_quote
        reply = handle_text_turn(self.db, self.user, "Show me the top 15 US companies.")
        self.assertIn("Top 15 U.S. companies", reply)
        self.assertIn("Fallback seed (not live)", reply)
        self.assertEqual(self.db.scalars(select(WatchlistItem)).all(), [])

    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_track_top_15_skips_invalid_and_creates_no_alerts(self, get_top, get_quote):
        get_top.return_value = ranking(self.companies)
        get_quote.side_effect = self.valid_quote
        reply = handle_text_turn(self.db, self.user, "Track the top 15 companies.")
        rows = self.db.scalars(select(WatchlistItem).order_by(WatchlistItem.id)).all()
        self.assertEqual(len(rows), 15)
        self.assertNotIn("SPCX", {row.symbol for row in rows})
        self.assertIn("Skipped symbols: SPCX", reply)
        self.assertEqual(self.db.scalars(select(Alert)).all(), [])

    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_biggest_10_and_existing_symbols_are_not_duplicated(self, get_top, get_quote):
        self.db.add(WatchlistItem(user_id=self.user.id, symbol="NVDA"))
        self.db.flush()
        get_top.return_value = ranking(self.companies)
        get_quote.side_effect = self.valid_quote
        handle_text_turn(self.db, self.user, "Track the biggest 10 companies.")
        symbols = self.db.scalars(select(WatchlistItem.symbol)).all()
        self.assertEqual(len(symbols), len(set(symbols)))
        self.assertEqual(symbols.count("NVDA"), 1)
        self.assertEqual(len(symbols), 10)

    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_goog_does_not_duplicate_existing_googl(self, get_top, get_quote):
        self.db.add(WatchlistItem(user_id=self.user.id, symbol="GOOGL"))
        self.db.flush()
        get_top.return_value = ranking([TopCompany("GOOG", "Alphabet")])
        get_quote.side_effect = self.valid_quote
        handle_text_turn(self.db, self.user, "Track the top 5 companies.")
        symbols = self.db.scalars(select(WatchlistItem.symbol)).all()
        self.assertEqual(symbols, ["GOOGL"])

    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_berkshire_symbol_is_normalized(self, get_top, get_quote):
        get_top.return_value = ranking([TopCompany("BRK.B", "Berkshire Hathaway")])
        get_quote.side_effect = self.valid_quote
        handle_text_turn(self.db, self.user, "Track the top 5 companies.")
        self.assertEqual(self.db.scalar(select(WatchlistItem.symbol)), "BRK-B")

    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_watchlist_limit_adds_only_available_capacity(self, get_top, get_quote):
        self.db.add_all([WatchlistItem(user_id=self.user.id, symbol=f"OLD{i}") for i in range(23)])
        self.db.flush()
        get_top.return_value = ranking(self.companies)
        get_quote.side_effect = self.valid_quote
        reply = handle_text_turn(self.db, self.user, "Track the top 5 companies.")
        self.assertEqual(len(self.db.scalars(select(WatchlistItem)).all()), 25)
        self.assertIn("25-company limit", reply)

    @patch("app.ai.brain.top_companies_service.companies_for_symbols")
    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_track_these_uses_previous_shown_ranking(self, get_top, get_quote, companies_for_symbols):
        shown = [TopCompany("NVDA", "NVIDIA"), TopCompany("AAPL", "Apple")]
        get_top.return_value = ranking(shown)
        get_quote.side_effect = self.valid_quote
        companies_for_symbols.return_value = shown
        handle_text_turn(self.db, self.user, "Show me the top 5 companies.")
        self.db.commit()
        reply = handle_text_turn(self.db, self.user, "Track these.")
        self.assertIn("Added: 2", reply)
        self.assertEqual(get_top.call_count, 1)
        self.assertEqual(set(self.db.scalars(select(WatchlistItem.symbol)).all()), {"NVDA", "AAPL"})

    @patch("app.ai.brain.top_companies_service.get_top", side_effect=RuntimeError("provider down"))
    def test_provider_failure_always_returns_response(self, _get_top):
        reply = handle_text_turn(self.db, self.user, "Show me the top 15 companies.")
        self.assertTrue(reply)
        self.assertIn("couldn't retrieve", reply)

    @patch("app.ai.brain.gateway.get_quote")
    @patch("app.ai.brain.top_companies_service.get_top")
    def test_remove_top_companies_preserves_unrelated_symbol(self, get_top, get_quote):
        self.db.add_all([
            WatchlistItem(user_id=self.user.id, symbol="NVDA"),
            WatchlistItem(user_id=self.user.id, symbol="PRIVATE"),
        ])
        self.db.flush()
        get_top.return_value = ranking(self.companies)
        get_quote.side_effect = self.valid_quote
        handle_text_turn(self.db, self.user, "Remove the top 15 companies from my watchlist.")
        self.assertEqual(self.db.scalars(select(WatchlistItem.symbol)).all(), ["PRIVATE"])


class LargeWatchlistBriefingTests(unittest.TestCase):
    @patch("app.services.briefing_service.gateway.get_earnings")
    @patch("app.services.briefing_service.gateway.get_news")
    @patch("app.services.briefing_service.gateway.get_quote")
    def test_large_watchlist_surfaces_only_largest_movers(self, get_quote, get_news, get_earnings):
        user = User(id=1, telegram_id=900, onboarding_stage=OnboardingStage.DONE.value)
        watchlist = [WatchlistItem(user_id=1, symbol=f"T{i:02d}") for i in range(15)]

        def quote(symbol, verify=False):
            move = float(int(symbol[1:]))
            return financial_result(QuoteData(symbol, 100, 99, move, move, "USD"))

        get_quote.side_effect = quote
        get_news.return_value = financial_result(None, DataStatus.UNAVAILABLE)
        get_earnings.return_value = financial_result(None, DataStatus.UNAVAILABLE)
        brief = build_morning_brief(user, watchlist, [])
        snapshot = brief.split("**WATCHLIST SNAPSHOT**", 1)[1].split("**ATLAS WATCH**", 1)[0]
        self.assertEqual(sum(1 for line in snapshot.splitlines() if line.startswith("• T")), 5)
        self.assertIn("T14", snapshot)
        self.assertNotIn("T00:", snapshot)
        self.assertIn("10 additional companies monitored.", snapshot)
        self.assertEqual(get_quote.call_count, 15)


if __name__ == "__main__":
    unittest.main()
