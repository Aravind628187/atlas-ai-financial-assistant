from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient
from sqlalchemy.engine import make_url

from app.api.server import create_dashboard_app
from app.config import Settings, runtime_port, settings
from app.database import create_database_engine, database_engine_options, normalize_database_url
from app import production
from app.production import validate_production_settings


class DatabaseConfigurationTests(unittest.TestCase):
    def test_sqlite_is_the_local_default(self):
        with patch.dict(os.environ, {}, clear=True):
            local = Settings(_env_file=None)
        self.assertEqual(local.database_url, "sqlite:///data/atlas.db")
        options = database_engine_options(local.database_url)
        self.assertEqual(options["connect_args"]["check_same_thread"], False)

    def test_postgresql_url_comes_from_environment(self):
        value = "postgresql://atlas:password@ep-example-pooler.neon.tech/atlas?sslmode=require"
        with patch.dict(os.environ, {"DATABASE_URL": value}, clear=True):
            production = Settings(_env_file=None)
        self.assertEqual(production.database_url, value)
        self.assertEqual(make_url(normalize_database_url(value)).get_backend_name(), "postgresql")
        options = database_engine_options(value)
        self.assertTrue(options["pool_pre_ping"])
        self.assertNotIn("connect_args", options)
        engine = create_database_engine(value)
        try:
            self.assertEqual(engine.dialect.name, "postgresql")
        finally:
            engine.dispose()

    def test_legacy_postgres_scheme_is_normalized_without_credentials_in_code(self):
        value = normalize_database_url("postgres://user:password@host/database")
        self.assertEqual(value, "postgresql://user:password@host/database")


class RuntimeModeTests(unittest.TestCase):
    def test_polling_is_the_local_default(self):
        with patch.dict(os.environ, {}, clear=True):
            local = Settings(_env_file=None)
        self.assertEqual(local.telegram_mode, "polling")

    def test_webhook_mode_builds_expected_url(self):
        configured = Settings(
            _env_file=None,
            telegram_mode="webhook",
            public_base_url="https://atlas-ai.onrender.com/",
        )
        self.assertEqual(configured.telegram_webhook_url, "https://atlas-ai.onrender.com/telegram/webhook")

    def test_render_port_overrides_local_port(self):
        self.assertEqual(runtime_port({"PORT": "10000"}), 10000)
        self.assertEqual(runtime_port({}), settings.dashboard_port)
        with self.assertRaises(ValueError):
            runtime_port({"PORT": "not-a-port"})

    def test_production_rejects_polling_and_accepts_complete_webhook_config(self):
        with patch.object(settings, "telegram_mode", "polling"):
            with self.assertRaisesRegex(RuntimeError, "TELEGRAM_MODE=webhook"):
                validate_production_settings()
        with patch.object(settings, "telegram_mode", "webhook"), \
             patch.object(settings, "telegram_bot_token", "test-token"), \
             patch.object(settings, "public_base_url", "https://atlas-ai.onrender.com"), \
             patch.object(settings, "telegram_webhook_secret", "safe_test-secret_123"):
            self.assertEqual(
                validate_production_settings(),
                "https://atlas-ai.onrender.com/telegram/webhook",
            )


class WebhookAndHealthTests(unittest.TestCase):
    def build_client(self, application=None):
        with patch("app.api.server.init_db"):
            return TestClient(create_dashboard_app(telegram_application=application))

    def test_webhook_is_not_enabled_in_polling_mode(self):
        client = self.build_client()
        with patch.object(settings, "telegram_mode", "polling"):
            response = client.post("/telegram/webhook", json={"update_id": 1})
        self.assertEqual(response.status_code, 404)

    def test_webhook_rejects_invalid_secret(self):
        application = Mock()
        application.update_queue = Mock()
        client = self.build_client(application)
        with patch.object(settings, "telegram_mode", "webhook"), \
             patch.object(settings, "telegram_webhook_secret", "expected-secret"):
            response = client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
                json={"update_id": 1},
            )
        self.assertEqual(response.status_code, 403)
        application.update_queue.put_nowait.assert_not_called()

    def test_webhook_dispatches_valid_update(self):
        application = Mock()
        application.bot = Mock()
        application.update_queue = Mock()
        client = self.build_client(application)
        parsed_update = Mock()
        with patch.object(settings, "telegram_mode", "webhook"), \
             patch.object(settings, "telegram_webhook_secret", "expected-secret"), \
             patch("app.api.server.Update.de_json", return_value=parsed_update):
            response = client.post(
                "/telegram/webhook",
                headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
                json={"update_id": 1},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        application.update_queue.put_nowait.assert_called_once_with(parsed_update)

    def test_health_is_public_and_leaks_no_secrets(self):
        client = self.build_client()
        sentinels = {
            "telegram_bot_token": "TOKEN-MUST-NOT-LEAK",
            "telegram_webhook_secret": "SECRET-MUST-NOT-LEAK",
            "database_url": "postgresql://user:DB-PASSWORD-MUST-NOT-LEAK@host/database",
        }
        with patch.object(settings, "telegram_bot_token", sentinels["telegram_bot_token"]), \
             patch.object(settings, "telegram_webhook_secret", sentinels["telegram_webhook_secret"]), \
             patch.object(settings, "database_url", sentinels["database_url"]):
            response = client.get("/api/health")
        self.assertEqual(response.status_code, 200)
        encoded = response.text
        for secret in sentinels.values():
            self.assertNotIn(secret, encoded)
        self.assertEqual(response.json()["service"], "atlas-ai")


class ProductionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_production_uses_webhook_without_starting_polling(self):
        telegram_app = Mock()
        telegram_app.initialize = AsyncMock()
        telegram_app.start = AsyncMock()
        telegram_app.stop = AsyncMock()
        telegram_app.shutdown = AsyncMock()
        telegram_app.bot = Mock()
        telegram_app.bot.set_webhook = AsyncMock()
        telegram_app.updater = Mock()
        telegram_app.updater.start_polling = AsyncMock()

        scheduler = Mock()
        scheduler.scheduler.running = True
        server = Mock()
        server.serve = AsyncMock()

        with patch.object(settings, "telegram_mode", "webhook"), \
             patch.object(settings, "telegram_bot_token", "test-token"), \
             patch.object(settings, "public_base_url", "https://atlas-ai.onrender.com"), \
             patch.object(settings, "telegram_webhook_secret", "safe-secret"), \
             patch("app.production.init_db"), \
             patch("app.production.build_application", return_value=telegram_app), \
             patch("app.production.AtlasScheduler", return_value=scheduler), \
             patch("app.production.create_dashboard_app", return_value=Mock()), \
             patch("app.production.runtime_port", return_value=10000), \
             patch("app.production.uvicorn.Config", return_value=Mock()), \
             patch("app.production.uvicorn.Server", return_value=server):
            await production.run()

        telegram_app.bot.set_webhook.assert_awaited_once()
        registered = telegram_app.bot.set_webhook.await_args.kwargs
        self.assertEqual(registered["url"], "https://atlas-ai.onrender.com/telegram/webhook")
        self.assertEqual(registered["secret_token"], "safe-secret")
        telegram_app.updater.start_polling.assert_not_awaited()
        scheduler.start.assert_called_once_with()
        server.serve.assert_awaited_once_with()
        telegram_app.stop.assert_awaited_once_with()
        telegram_app.shutdown.assert_awaited_once_with()


if __name__ == "__main__":
    unittest.main()
