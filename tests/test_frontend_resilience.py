from __future__ import annotations

import datetime as dt
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.server import clear_public_cache, create_dashboard_app
from app.services.providers.base import DataStatus, FinancialDataResult, QuoteData
from app.services.top_companies_service import TopCompaniesRanking, TopCompany


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


class FrontendResilienceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("app.api.server.init_db"):
            cls.client = TestClient(create_dashboard_app())

    def setUp(self):
        clear_public_cache()

    def test_retry_cache_timeout_and_deduplication_node_suite(self):
        completed = subprocess.run(
            ["node", "tests/frontend_resilience_node_test.js"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_page_has_manual_visibility_offline_and_independent_refresh_controls(self):
        html = Path("frontend/index.html").read_text(encoding="utf-8")
        javascript = Path("frontend/app.js").read_text(encoding="utf-8")
        self.assertIn('id="refresh-data"', html)
        self.assertIn("manualRefresh", javascript)
        self.assertIn('document.addEventListener("visibilitychange"', javascript)
        self.assertIn('window.addEventListener("offline"', javascript)
        self.assertIn('window.addEventListener("online"', javascript)
        self.assertIn("document.hidden", javascript)
        self.assertIn("navigator.onLine", javascript)
        self.assertIn("refreshSection(name", javascript)
        self.assertNotIn("window.location.reload", javascript)
        self.assertNotIn("location.reload", javascript)

    def test_top_companies_keeps_all_ranks_when_one_quote_fails(self):
        ranking = TopCompaniesRanking(
            (
                TopCompany("NVDA", "NVIDIA"),
                TopCompany("MSFT", "Microsoft"),
                TopCompany("AAPL", "Apple"),
            ),
            "fmp",
            NOW,
            True,
        )

        def quote(symbol: str) -> FinancialDataResult:
            available = symbol != "MSFT"
            data = QuoteData(symbol, 100, currency="USD") if available else None
            return FinancialDataResult(
                DataStatus.OK if available else DataStatus.UNAVAILABLE,
                "finnhub" if available else "none",
                "quote",
                NOW,
                NOW if available else None,
                symbol,
                data,
                freshness="live" if available else "unavailable",
                market_status="open",
            )

        with patch("app.api.server.top_companies_service.get_top", return_value=ranking), patch(
            "app.api.server.gateway.get_quote", side_effect=quote,
        ):
            payload = self.client.get("/api/public/top-companies?limit=3").json()
        self.assertEqual([row["rank"] for row in payload["companies"]], [1, 2, 3])
        self.assertEqual(len(payload["companies"]), 3)
        self.assertFalse(payload["companies"][1]["quote_available"])
        self.assertIsNone(payload["companies"][1]["price"])

    def test_market_overview_keeps_successful_cards_when_one_quote_fails(self):
        def quote(symbol: str) -> FinancialDataResult:
            available = symbol != "MSFT"
            return FinancialDataResult(
                DataStatus.OK if available else DataStatus.UNAVAILABLE,
                "twelve_data" if available else "none",
                "quote",
                NOW,
                NOW if available else None,
                symbol,
                QuoteData(symbol, 100, currency="USD") if available else None,
                freshness="delayed" if available else "unavailable",
                market_status="closed",
            )

        with patch("app.api.server.gateway.get_quote", side_effect=quote):
            payload = self.client.get("/api/public/market-overview").json()
        self.assertEqual(len(payload["quotes"]), 4)
        self.assertEqual(sum(row["available"] for row in payload["quotes"]), 3)
        self.assertIsNone(next(row for row in payload["quotes"] if row["symbol"] == "MSFT")["price"])

    def test_provider_summary_can_transition_from_limited_to_connected(self):
        limited = {"router": {"providers": {"finnhub": {"configured": True, "status": "rate_limited"}}}}
        connected = {"router": {"providers": {"finnhub": {"configured": True, "status": "ok"}}}}
        with patch("app.api.server.gateway.health", side_effect=[limited, connected]):
            first = self.client.get("/api/public/provider-summary").json()
            second = self.client.get("/api/public/provider-summary").json()
        self.assertEqual(first["providers"][0]["status"], "Limited")
        self.assertEqual(second["providers"][0]["status"], "Connected")

    def test_public_api_exposes_cached_quote_as_last_verified_not_live(self):
        cached = FinancialDataResult(
            DataStatus.STALE,
            "finnhub",
            "quote",
            NOW,
            NOW - dt.timedelta(days=1),
            "NVDA",
            QuoteData("NVDA", 100, currency="USD"),
            is_realtime=False,
            is_delayed=True,
            is_stale=True,
            verification={"cached_verified": True},
            freshness="stale",
            market_status="closed",
        )
        with patch("app.api.server.gateway.get_quote", return_value=cached):
            payload = self.client.get("/api/public/market-overview").json()
        self.assertTrue(payload["quotes"][0]["available"])
        self.assertEqual(payload["quotes"][0]["freshness"], "last_verified")
        self.assertFalse(cached.is_realtime)


if __name__ == "__main__":
    unittest.main()
