"""
Single-command entry point for Atlas AI.

Runs three things concurrently in one asyncio loop:
  1. The Telegram bot (long polling)
  2. The background scheduler (daily briefings + price alerts)
  3. The dashboard API + static UI (FastAPI/uvicorn)

    python -m app.main
"""
from __future__ import annotations

import asyncio
import logging
import socket

import uvicorn
from telegram.error import NetworkError, TelegramError

from app.api.server import create_dashboard_app
from app.bot.telegram_bot import build_application, send_message
from app.config import settings
from app.database import init_db
from app.services.scheduler import AtlasScheduler
from app.services.runtime_state import runtime_state
from app.utils.logger import setup_logging

logger = logging.getLogger("atlas.main")


def _dashboard_port_is_busy() -> bool:
    """Fail cleanly before Telegram/scheduler startup when Atlas already runs."""
    host = "127.0.0.1" if settings.dashboard_host in {"0.0.0.0", "::"} else settings.dashboard_host
    try:
        with socket.create_connection((host, settings.dashboard_port), timeout=0.35):
            return True
    except OSError:
        return False


async def run() -> None:
    setup_logging()
    if settings.telegram_mode != "polling":
        logger.error(
            "TELEGRAM_MODE=%s does not use the polling launcher; run scripts/run_production.py",
            settings.telegram_mode,
        )
        return
    init_db()
    if not settings.gemini_api_key:
        logger.warning("⚠️  GEMINI_API_KEY is empty — set it in .env before chatting with the bot.")
    if not settings.telegram_bot_token:
        logger.error("❌ TELEGRAM_BOT_TOKEN is empty — the bot cannot start without it. Add it to .env.")
        return
    if _dashboard_port_is_busy():
        logger.error(
            "Atlas is already running on port %s. Stop the existing process before starting another copy.",
            settings.dashboard_port,
        )
        return

    telegram_app = build_application()

    async def push(telegram_id: int, text: str) -> None:
        await send_message(telegram_app, telegram_id, text)

    scheduler = AtlasScheduler(push)

    dashboard_app = create_dashboard_app()
    uv_config = uvicorn.Config(
        dashboard_app, host=settings.dashboard_host, port=settings.dashboard_port, log_level="warning"
    )
    server = uvicorn.Server(uv_config)

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling(drop_pending_updates=True)
    scheduler.start()

    logger.info("🚀 Atlas AI is live")
    logger.info("   • Telegram bot: polling for messages")
    logger.info("   • Dashboard:    http://%s:%s", settings.dashboard_host, settings.dashboard_port)

    try:
        await server.serve()
    finally:
        logger.info("Shutting down Atlas AI...")
        scheduler.scheduler.shutdown(wait=False)
        runtime_state.scheduler_running = False
        await telegram_app.updater.stop()
        await telegram_app.stop()
        await telegram_app.shutdown()


def main() -> None:
    try:
        asyncio.run(run())
    except NetworkError:
        logger.error(
            "Telegram is temporarily unreachable. Check the internet/DNS connection and run Atlas again."
        )
    except TelegramError as exc:
        logger.error("Telegram startup failed: %s", exc)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
