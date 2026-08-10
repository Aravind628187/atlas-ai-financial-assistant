from __future__ import annotations

import datetime as dt
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.api.server import clear_public_cache, create_dashboard_app
from app.services.providers.base import DataStatus, FinancialDataResult, QuoteData
from app.services.top_companies_service import TopCompaniesRanking, TopCompany


UTC = dt.timezone.utc
NOW = dt.datetime(2026, 8, 10, 8, 0, tzinfo=UTC)


def quote_result(symbol: str, price: float | None = 100.0) -> FinancialDataResult:
    data = QuoteData(
        symbol=symbol,
        price=price,
        previous_close=99.0 if price is not None else None,
        change=1.0 if price is not None else None,
        change_pct=1.01 if price is not None else None,
        currency="USD" if price is not None else None,
        name=f"{symbol} Company",
    ) if price is not None else None
    return FinancialDataResult(
        status=DataStatus.OK if data else DataStatus.UNAVAILABLE,
        source="finnhub",
        source_type="market_data",
        retrieved_at=NOW,
        data_as_of=NOW if data else None,
        symbol=symbol,
        data=data,
        is_realtime=True,
        is_delayed=False,
        freshness="live" if data else "unavailable",
        market_status="open",
    )


class PublicFrontendApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with patch("app.api.server.init_db"):
            cls.client = TestClient(create_dashboard_app())

    def setUp(self):
        clear_public_cache()

    def test_public_routes_need_no_admin_session_but_private_api_does(self):
        health = {"router": {"providers": {}}}
        with patch("app.api.server.gateway.health", return_value=health):
            self.assertEqual(self.client.get("/api/public/provider-summary").status_code, 200)
        self.assertEqual(self.client.get("/api/public/system-summary").status_code, 200)
        self.assertEqual(self.client.get("/api/public/telegram-qr").status_code, 200)
        self.assertEqual(self.client.get("/api/overview").status_code, 401)

    def test_telegram_qr_is_public_svg_for_configured_bot(self):
        response = self.client.get("/api/public/telegram-qr")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("image/svg+xml"))
        self.assertIn(b"<svg", response.content)
        self.assertNotIn(b"token", response.content.lower())

    def test_market_overview_returns_only_safe_normalized_quote_fields(self):
        with patch("app.api.server.gateway.get_quote", side_effect=lambda symbol: quote_result(symbol)):
            response = self.client.get("/api/public/market-overview")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["symbol"] for item in payload["quotes"]], ["NVDA", "MSFT", "AAPL", "GOOGL"])
        self.assertTrue(all(item["available"] for item in payload["quotes"]))
        self.assertEqual(payload["quotes"][0]["price"], 100.0)
        self.assertNotIn("error", json.dumps(payload).lower())

    def test_unavailable_quote_never_exposes_a_price(self):
        with patch("app.api.server.gateway.get_quote", side_effect=lambda symbol: quote_result(symbol, None)):
            payload = self.client.get("/api/public/market-overview").json()
        for item in payload["quotes"]:
            self.assertFalse(item["available"])
            self.assertIsNone(item["price"])
            self.assertIsNone(item["source"])

    def test_top_companies_labels_live_and_reference_rankings(self):
        live = TopCompaniesRanking((TopCompany("MSFT", "Microsoft"),), "fmp", NOW, True)
        with patch("app.api.server.top_companies_service.get_top", return_value=live), \
             patch("app.api.server.gateway.get_quote", return_value=quote_result("MSFT")):
            payload = self.client.get("/api/public/top-companies?limit=15").json()
        self.assertTrue(payload["is_live_ranking"])
        self.assertEqual(payload["source"], "FMP")
        self.assertEqual(payload["companies"][0]["symbol"], "MSFT")

        clear_public_cache()
        reference = TopCompaniesRanking((TopCompany("NVDA", "NVIDIA"),), "fallback_seed", NOW, False)
        with patch("app.api.server.top_companies_service.get_top", return_value=reference), \
             patch("app.api.server.gateway.get_quote", return_value=quote_result("NVDA", None)):
            payload = self.client.get("/api/public/top-companies?limit=15").json()
        self.assertFalse(payload["is_live_ranking"])
        self.assertEqual(payload["source"], "fallback_seed")
        self.assertFalse(payload["companies"][0]["quote_available"])

    def test_provider_summary_is_coarse_and_contains_no_operational_details(self):
        health = {
            "router": {"providers": {
                "finnhub": {"configured": True, "status": "ok", "latency_ms": 27, "last_error": "secret detail"},
                "fmp": {"configured": True, "status": "rate_limited", "failure_count": 4},
            }}
        }
        with patch("app.api.server.gateway.health", return_value=health):
            payload = self.client.get("/api/public/provider-summary").json()
        self.assertEqual(payload["providers"][0]["status"], "Connected")
        encoded = json.dumps(payload).lower()
        for forbidden in ("latency", "last_error", "failure_count", "secret detail", "api_key", "token"):
            self.assertNotIn(forbidden, encoded)

    def test_every_public_response_excludes_private_user_fields_and_secrets(self):
        health = {"router": {"providers": {"finnhub": {"configured": True, "status": "ok"}}}}
        ranking = TopCompaniesRanking((TopCompany("MSFT", "Microsoft"),), "fmp", NOW, True)
        with patch("app.api.server.gateway.health", return_value=health), \
             patch("app.api.server.gateway.get_quote", return_value=quote_result("MSFT")), \
             patch("app.api.server.top_companies_service.get_top", return_value=ranking):
            responses = [
                self.client.get("/api/public/system-summary").json(),
                self.client.get("/api/public/provider-summary").json(),
                self.client.get("/api/public/market-overview").json(),
                self.client.get("/api/public/top-companies").json(),
            ]
        encoded = json.dumps(responses).lower()
        for forbidden in ("user_id", "telegram_id", "email", "message_text", "document_path", "api_key", "secret_key"):
            self.assertNotIn(forbidden, encoded)

    def test_public_page_contains_dynamic_mounts_without_legacy_fake_quotes(self):
        html = Path("frontend/index.html").read_text(encoding="utf-8")
        for mount in ("market-grid", "companies-grid", "provider-list", "market-tape-track"):
            self.assertIn(f'id="{mount}"', html)
        for legacy_value in ("223.96", "2.07%", "const BOT_USERNAME"):
            self.assertNotIn(legacy_value, html)


if __name__ == "__main__":
    unittest.main()
