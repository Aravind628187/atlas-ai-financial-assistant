"""Single-process Render runtime for FastAPI, Telegram webhooks, and Atlas jobs."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urlsplit

import uvicorn
from telegram import Update
from telegram.error import NetworkError, TelegramError

from app.api.server import create_dashboard_app
from app.bot.telegram_bot import build_application, send_message
from app.config import runtime_port, settings
from app.database import init_db
from app.services.runtime_state import runtime_state
from app.services.scheduler import AtlasScheduler
from app.utils.logger import setup_logging


logger = logging.getLogger("atlas.production")
WEBHOOK_SECRET_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,256}$")


def validate_production_settings() -> str:
    """Fail closed before binding a public server with incomplete webhook config."""
    if settings.telegram_mode != "webhook":
        raise RuntimeError("Production requires TELEGRAM_MODE=webhook")
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    webhook_url = settings.telegram_webhook_url
    parsed = urlsplit(webhook_url or "")
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("PUBLIC_BASE_URL must be a public HTTPS URL")
    if not WEBHOOK_SECRET_PATTERN.fullmatch(settings.telegram_webhook_secret):
        raise RuntimeError("TELEGRAM_WEBHOOK_SECRET must use 1-256 URL-safe characters")
    return webhook_url


async def run() -> None:
    setup_logging()
    webhook_url = validate_production_settings()
    init_db()

    telegram_app = build_application()

    async def push(telegram_id: int, text: str) -> None:
        await send_message(telegram_app, telegram_id, text)

    scheduler = AtlasScheduler(push)
    api = create_dashboard_app(telegram_application=telegram_app)
    server = uvicorn.Server(uvicorn.Config(
        api,
        host="0.0.0.0",
        port=runtime_port(),
        log_level="info",
    ))

    initialized = False
    started = False
    scheduler_started = False
    try:
        await telegram_app.initialize()
        initialized = True
        await telegram_app.start()
        started = True
        await telegram_app.bot.set_webhook(
            url=webhook_url,
            secret_token=settings.telegram_webhook_secret,
            allowed_updates=Update.ALL_TYPES,
        )
        scheduler.start()
        scheduler_started = True
        logger.info("Atlas production runtime started (FastAPI + Telegram webhook + scheduler)")
        await server.serve()
    finally:
        logger.info("Shutting down Atlas production runtime")
        if scheduler_started and scheduler.scheduler.running:
            scheduler.scheduler.shutdown(wait=False)
        runtime_state.scheduler_running = False
        if started:
            await telegram_app.stop()
        if initialized:
            await telegram_app.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except (NetworkError, TelegramError) as exc:
        logger.error("Telegram production startup failed: %s", type(exc).__name__)
        raise SystemExit(1) from exc
    except (RuntimeError, ValueError) as exc:
        logger.error("Production configuration error: %s", exc)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
