from __future__ import annotations

import datetime as dt
import unittest
from unittest.mock import Mock, patch

from google.api_core.exceptions import ResourceExhausted

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.ai.brain import handle_text_turn
from app.ai.gemini_client import GeminiClient, GeminiUnavailableError
from app.ai.financial_response_validator import validate_financial_response
from app.ai.intent_router import route
from app.database import Base
from app.models import Alert, OnboardingStage, User, WatchlistItem
from app.services.financial_data_gateway import FinancialDataGateway
from app.services.document_service import answer_question_about_document
from app.services.briefing_service import build_morning_brief
from app.services.providers.base import (
    DataStatus, EarningsData, FilingItem, FilingsData, FinancialDataResult, FundamentalsData,
    HistoricalData, HistoricalPoint, NewsItem, QuoteData,
)
from app.services.providers.sec_provider import SECProvider


UTC = dt.timezone.utc


def result(data=None, status=DataStatus.OK, source="test-feed"):
    now = dt.datetime.now(UTC)
    return FinancialDataResult(
        status=status, source=source, source_type="market_data", retrieved_at=now,
        data_as_of=now, symbol=getattr(data, "symbol", "NVDA"), data=data,
        is_realtime=False, is_delayed=True,
        verification={"freshness": "delayed", "market_session": "open"},
    )


class BrainReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.user = User(telegram_id=99, onboarding_stage=OnboardingStage.DONE.value)
        self.db.add(self.user)
        self.db.flush()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @patch("app.ai.brain.gateway.get_quote")
    def test_quote_uses_only_provider_value(self, get_quote):
        get_quote.return_value = result(QuoteData("NVDA", 223.96, 219.42, 4.54, 2.07, "USD"))
        reply = handle_text_turn(self.db, self.user, "What's Nvidia's price?")
        self.assertIn("223.96", reply)
        self.assertIn("test-feed", reply)
        self.assertIn("As of", reply)

    @patch("app.ai.brain.gateway.get_quote")
    def test_market_closed_wording_is_explicit(self, get_quote):
        value = result(QuoteData("MSFT", 500, 495, 5, 1.01, "USD"))
        value.verification["market_session"] = "closed"
        get_quote.return_value = value
        reply = handle_text_turn(self.db, self.user, "How is Microsoft performing today?")
        self.assertIn("Market status: Closed", reply)
        self.assertIn("The US market is currently closed.", reply)
        self.assertNotIn("currently trading", reply.lower())

    @patch("app.ai.brain.gateway.get_fundamentals")
    def test_dividend_definition_labels_and_unknown_omission(self, fundamentals):
        fundamentals.return_value = result(FundamentalsData(
            "NVDA", dividend_yield=0.0045, dividend_yield_forward=0.0045,
            metric_definitions={"dividend_yield": "forward"},
        ))
        reply = handle_text_turn(self.db, self.user, "What is Nvidia's dividend yield?")
        self.assertIn("Forward dividend yield", reply)
        fundamentals.return_value = result(FundamentalsData("NVDA", dividend_yield=0.0013))
        unknown = handle_text_turn(self.db, self.user, "What is Nvidia's dividend yield?")
        self.assertNotIn("Dividend yield: **0.13%", unknown)

    @patch("app.ai.brain.gateway.get_earnings")
    def test_unverified_earnings_reply_suppresses_provider_date(self, earnings):
        earnings.return_value = result(EarningsData(
            "MSFT", dt.datetime(2027, 1, 26, tzinfo=UTC), status="unverified", source="fmp"
        ))
        reply = handle_text_turn(self.db, self.user, "When does Microsoft report earnings?")
        self.assertIn("Earnings date: not yet verified.", reply)
        self.assertNotIn("2027", reply)

    @patch("app.ai.brain.gateway.get_earnings")
    def test_estimated_earnings_uses_corroborated_wording(self, earnings):
        earnings.return_value = result(EarningsData(
            "MSFT", dt.datetime(2026, 10, 26, tzinfo=UTC), status="estimated",
            source="finnhub", verified_with="alpha_vantage",
        ))
        reply = handle_text_turn(self.db, self.user, "When does Microsoft report earnings?")
        self.assertIn("Estimate corroborated by: Alpha Vantage", reply)
        self.assertNotIn("Verified with: Alpha Vantage", reply)

    @patch("app.ai.brain.gateway.get_quote")
    def test_exact_price_request_refuses_when_unavailable(self, get_quote):
        get_quote.return_value = result(None, DataStatus.UNAVAILABLE, "none")
        reply = handle_text_turn(self.db, self.user, "Tell me Nvidia's exact price even if data isn't available.")
        self.assertIn("won't provide an exact price", reply)
        self.assertNotRegex(reply, r"\$\d")

    @patch("app.ai.brain.gateway.get_news")
    @patch("app.ai.brain.gateway.get_quote")
    def test_no_news_means_no_invented_catalyst(self, get_quote, get_news):
        get_quote.return_value = result(QuoteData("NVDA", 223.96, 219.42, 4.54, 2.07, "USD"))
        get_news.return_value = result(None, DataStatus.UNAVAILABLE, "none")
        reply = handle_text_turn(self.db, self.user, "What's moving Nvidia today?")
        self.assertIn("couldn't verify a specific recent catalyst", reply)
        self.assertNotIn("investment", reply.lower())

    @patch("app.ai.brain.gateway.get_news")
    @patch("app.ai.brain.gateway.get_quote")
    def test_related_news_is_not_called_a_catalyst(self, get_quote, get_news):
        now = dt.datetime.now(UTC)
        get_quote.return_value = result(QuoteData("NVDA", 223.96, 219.42, 4.54, 2.07, "USD"))
        get_news.return_value = result([NewsItem("1", "Nvidia announces a new platform", "Wire", now)])
        reply = handle_text_turn(self.db, self.user, "What's moving Nvidia today?")
        self.assertIn("Relevant developments", reply)
        self.assertIn("couldn't verify a specific catalyst", reply)

    def test_concentration_request_is_risk_aware(self):
        reply = handle_text_turn(self.db, self.user, "Should I put all my money into Nvidia?")
        self.assertIn("concentration risk", reply)
        self.assertNotIn("definitely", reply.lower())

    @patch("app.ai.brain.gateway.get_quote")
    def test_alert_create_list_update_remove(self, get_quote):
        get_quote.return_value = result(QuoteData("NVDA", 100, 99, 1, 1.01, "USD"))
        created = handle_text_turn(self.db, self.user, "Alert me when Nvidia moves more than 5%.")
        self.assertIn("5%", created)
        alert = self.db.scalar(select(Alert).where(Alert.user_id == self.user.id))
        self.assertIsNotNone(alert)
        listed = handle_text_turn(self.db, self.user, "What alerts do I have?")
        self.assertIn("NVDA", listed)
        updated = handle_text_turn(self.db, self.user, "Change my Nvidia alert to 3%.")
        self.assertIn("3%", updated)
        self.assertEqual(alert.threshold_pct, 3.0)
        removed = handle_text_turn(self.db, self.user, "Remove my Nvidia alert.")
        self.assertIn("Turned off", removed)
        self.assertFalse(alert.active)

    def test_watchlist_show_reads_database(self):
        self.db.add(WatchlistItem(user_id=self.user.id, symbol="AAPL"))
        self.db.flush()
        reply = handle_text_turn(self.db, self.user, "Show my watchlist.")
        self.assertIn("AAPL", reply)

    def test_watchlist_clear_removes_all_items(self):
        self.db.add_all([
            WatchlistItem(user_id=self.user.id, symbol="AAPL", label="Core"),
            WatchlistItem(user_id=self.user.id, symbol="NVDA"),
        ])
        self.db.flush()
        reply = handle_text_turn(self.db, self.user, "Clear my watchlist.")
        self.assertIn("Cleared", reply)
        self.assertEqual(self.db.scalars(select(WatchlistItem)).all(), [])

    def test_alert_disable_all_and_restore(self):
        self.db.add_all([
            Alert(user_id=self.user.id, symbol="AAPL", threshold_pct=4, active=True),
            Alert(user_id=self.user.id, symbol="NVDA", threshold_pct=5, active=True),
        ])
        self.db.flush()
        self.assertIn("**2** alerts", handle_text_turn(self.db, self.user, "Turn off all alerts."))
        self.assertTrue(all(not row.active for row in self.db.scalars(select(Alert)).all()))
        self.assertIn("**2** alerts", handle_text_turn(self.db, self.user, "Turn alerts back on."))
        self.assertTrue(all(row.active for row in self.db.scalars(select(Alert)).all()))

    def test_natural_alert_edit_without_alert_keyword(self):
        row = Alert(user_id=self.user.id, symbol="NVDA", threshold_pct=5, active=True)
        self.db.add(row)
        self.db.flush()
        reply = handle_text_turn(self.db, self.user, "Change Nvidia to 3%.")
        self.assertIn("3%", reply)
        self.assertEqual(row.threshold_pct, 3)

    def test_profit_is_deterministic(self):
        reply = handle_text_turn(self.db, self.user, "If I bought 10 shares at $100 and sold them at $125, what is my profit?")
        self.assertIn("$250.00", reply)

    def test_percentage_gain_is_deterministic(self):
        reply = handle_text_turn(self.db, self.user, "Calculate 12% gain on $10000.")
        self.assertIn("$1,200.00", reply)

    @patch("app.ai.brain.gateway.get_fundamentals")
    def test_comparison_follow_up_uses_conversation_context(self, fundamentals):
        def lookup(symbol):
            pe = 30.0 if symbol == "NVDA" else 20.0
            return result(FundamentalsData(symbol, trailing_pe=pe))
        fundamentals.side_effect = lookup
        first = handle_text_turn(self.db, self.user, "Compare Nvidia and AMD.")
        self.db.commit()
        self.assertIn("NVDA", first)
        follow_up = handle_text_turn(self.db, self.user, "Which one has the higher valuation?")
        self.assertIn("NVDA", follow_up)
        self.assertIn("higher", follow_up)

    @patch("app.ai.brain.gateway.get_fundamentals")
    def test_margin_follow_up_keeps_both_companies(self, fundamentals):
        fundamentals.side_effect = lambda symbol: result(FundamentalsData(
            symbol, profit_margin=0.25 if symbol == "NVDA" else 0.12
        ))
        handle_text_turn(self.db, self.user, "Compare Nvidia and AMD.")
        self.db.commit()
        reply = handle_text_turn(self.db, self.user, "What about margins?")
        self.assertIn("NVDA", reply)
        self.assertIn("AMD", reply)
        self.assertIn("profit margin", reply)

    @patch("app.ai.brain.gateway.get_history")
    def test_historical_reply_calculates_max_drawdown(self, history):
        now = dt.datetime.now(UTC)
        points = [HistoricalPoint(now + dt.timedelta(days=i), close=value) for i, value in enumerate((100, 120, 90, 110))]
        history.return_value = result(HistoricalData("AAPL", points))
        reply = handle_text_turn(self.db, self.user, "Show Apple's six month drawdown.")
        self.assertIn("Maximum close-to-close drawdown", reply)
        self.assertIn("-25.00%", reply)

    @patch("app.ai.brain.gateway.get_filings")
    def test_sec_filing_reply_is_grounded(self, filings):
        item = FilingItem("AAPL", "Apple Inc.", "10-Q", dt.date(2026, 8, 1), report_date=dt.date(2026, 6, 30), url="https://www.sec.gov/example")
        older = FilingItem("AAPL", "Apple Inc.", "10-Q", dt.date(2026, 5, 1), url="https://www.sec.gov/older")
        filings.return_value = result(FilingsData("AAPL", "0000320193", "Apple Inc.", [item]), source="SEC EDGAR")
        reply = handle_text_turn(self.db, self.user, "Show Apple's latest 10-Q.")
        self.assertIn("SEC EDGAR", reply)
        self.assertIn("2026-08-01", reply)
        self.assertIn("Reporting period:", reply)
        self.assertIn("Ask me to summarize it", reply)
        filings.assert_called_once_with("AAPL", ("10-Q",), 1)

    @patch("app.services.briefing_service.gateway.get_news")
    @patch("app.services.briefing_service.gateway.get_quote")
    @patch("app.services.briefing_service.gateway.get_earnings")
    def test_briefing_separates_market_and_news_timestamps(self, get_earnings, get_quote, get_news):
        market_time = dt.datetime(2026, 8, 7, 20, 0, tzinfo=UTC)
        news_time = dt.datetime(2026, 8, 10, 9, 30, tzinfo=UTC)
        quote_result = result(QuoteData("NVDA", 223.96, 218.99, 4.97, 2.27, "USD"))
        quote_result.data_as_of = market_time
        news_result = result([NewsItem("1", "Nvidia announces new AI platform", "Wire", news_time)])
        news_result.data_as_of = news_time
        get_quote.return_value = quote_result
        get_news.return_value = news_result
        get_earnings.return_value = result(None, DataStatus.UNAVAILABLE, "none")
        brief = build_morning_brief(self.user, [WatchlistItem(user_id=self.user.id, symbol="NVDA")], [])
        self.assertIn("Market data — latest verified session: 2026-08-07", brief)
        self.assertIn("News updated through: 2026-08-10 09:30 UTC", brief)

    @patch("app.services.briefing_service.gateway.get_earnings")
    @patch("app.services.briefing_service.gateway.get_news")
    @patch("app.services.briefing_service.gateway.get_quote")
    def test_briefing_omits_unverified_earnings_date(self, get_quote, get_news, get_earnings):
        get_quote.return_value = result(QuoteData("MSFT", 500, 495, 5, 1.01, "USD"))
        get_news.return_value = result(None, DataStatus.UNAVAILABLE, "none")
        get_earnings.return_value = result(EarningsData(
            "MSFT", dt.datetime(2027, 1, 26, tzinfo=UTC), status="unverified", source="fmp"
        ))
        brief = build_morning_brief(self.user, [WatchlistItem(user_id=self.user.id, symbol="MSFT")], [])
        self.assertNotIn("UPCOMING", brief)
        self.assertNotIn("2027", brief)

    @patch("app.services.briefing_service.gateway.get_earnings")
    @patch("app.services.briefing_service.gateway.get_news")
    @patch("app.services.briefing_service.gateway.get_quote")
    def test_briefing_labels_estimated_earnings(self, get_quote, get_news, get_earnings):
        get_quote.return_value = result(QuoteData("MSFT", 500, 495, 5, 1.01, "USD"))
        get_news.return_value = result(None, DataStatus.UNAVAILABLE, "none")
        get_earnings.return_value = result(EarningsData(
            "MSFT", dt.datetime(2026, 10, 26, tzinfo=UTC), status="estimated",
            source="fmp", verified_with="alpha_vantage"
        ))
        brief = build_morning_brief(self.user, [WatchlistItem(user_id=self.user.id, symbol="MSFT")], [])
        self.assertIn("Estimated earnings: Oct 26, 2026", brief)
        self.assertIn("estimate corroborated by Alpha Vantage", brief)
        self.assertNotIn("verified with Alpha Vantage", brief)
        self.assertNotIn("MSFT earnings:", brief)

    def test_ambiguous_company_request_clarifies(self):
        reply = handle_text_turn(self.db, self.user, "Tell me about Apple.")
        self.assertIn("price", reply)
        self.assertIn("fundamentals", reply)

    def test_static_definition_needs_no_market_number(self):
        reply = handle_text_turn(self.db, self.user, "Explain P/E ratio like I'm a beginner.")
        self.assertIn("earnings per share", reply)

    @patch("app.ai.brain.gemini.generate", side_effect=TimeoutError("quota"))
    def test_gemini_timeout_does_not_crash(self, _generate):
        reply = handle_text_turn(self.db, self.user, "What is cloud computing?")
        self.assertIn("can't reach", reply)


class GuardTests(unittest.TestCase):
    def test_sec_provider_normalizes_official_submission(self):
        client = Mock()
        ticker_response = Mock()
        ticker_response.json.return_value = {"0": {"ticker": "AAPL", "cik_str": 320193, "title": "Apple Inc."}}
        ticker_response.raise_for_status.return_value = None
        filing_response = Mock()
        filing_response.json.return_value = {
            "name": "Apple Inc.",
            "filings": {"recent": {
                "form": ["10-Q"], "filingDate": ["2026-08-01"], "reportDate": ["2026-06-30"],
                "accessionNumber": ["0000320193-26-000001"], "primaryDocument": ["aapl-20260630.htm"],
            }},
        }
        filing_response.raise_for_status.return_value = None
        client.get.side_effect = [ticker_response, filing_response]
        SECProvider._ticker_cache = None
        value = SECProvider(client=client).get_filings("AAPL", ("10-Q",), 1)
        self.assertEqual(value.status, DataStatus.OK)
        self.assertEqual(value.data.filings[0].form, "10-Q")
        self.assertIn("Archives/edgar/data/320193/", value.data.filings[0].url)

    def test_gemini_quota_opens_circuit_without_retry_storm(self):
        model = Mock()
        model.generate_content.side_effect = ResourceExhausted("quota exceeded")
        with patch("app.ai.gemini_client.settings.gemini_api_key", "test-key"), patch(
            "app.ai.gemini_client.genai.GenerativeModel", return_value=model
        ):
            client = GeminiClient("test-model")
            with self.assertRaises(GeminiUnavailableError):
                client.generate("hello")
            self.assertEqual(model.generate_content.call_count, 1)
            with self.assertRaises(GeminiUnavailableError):
                client.generate("hello again")
            self.assertEqual(model.generate_content.call_count, 1)

    def test_numeric_hallucination_validator(self):
        verdict = validate_financial_response("NVDA is $223.96 and up 2.07%", [223.96])
        self.assertFalse(verdict.valid)
        self.assertIn("2.07%", verdict.unsupported_claims)

    @patch("app.services.document_service.gemini.generate", return_value="Revenue was $9 billion.")
    def test_document_answer_blocks_number_absent_from_document(self, _generate):
        reply = answer_question_about_document("Revenue was discussed without an amount.", "What was revenue?")
        self.assertIn("won't guess", reply)

    @patch("app.ai.intent_router.gemini.generate_json", return_value={"intent": "made_up", "symbols": "bad"})
    def test_bad_gemini_json_is_rejected(self, _json):
        self.assertEqual(route("an ambiguous portfolio request").intent, "unsupported_or_uncertain")

    def test_provider_timeout_fails_closed(self):
        class TimeoutProvider:
            name = "timeout-provider"
            def get_quote(self, symbol):
                raise TimeoutError("provider timed out")

        gateway = FinancialDataGateway(primary=TimeoutProvider(), secondary=None)
        gateway.secondary = None
        with patch.object(gateway, "_log_fetch"):
            value = gateway.get_quote("NVDA")
        self.assertEqual(value.status, DataStatus.UNAVAILABLE)
        self.assertIsNone(value.data)

    def test_provider_disagreement_fails_closed(self):
        class Provider:
            def __init__(self, name, price):
                self.name, self.price = name, price
            def get_quote(self, symbol):
                return result(QuoteData(symbol, self.price, 100, self.price - 100, self.price - 100), source=self.name)

        gateway = FinancialDataGateway(primary=Provider("primary", 100), secondary=Provider("secondary", 110))
        with patch.object(gateway, "_log_fetch"):
            value = gateway.get_quote("NVDA")
        self.assertEqual(value.status, DataStatus.UNAVAILABLE)
        self.assertIsNone(value.data)
        self.assertTrue(value.verification["disagreement"])


if __name__ == "__main__":
    unittest.main()
