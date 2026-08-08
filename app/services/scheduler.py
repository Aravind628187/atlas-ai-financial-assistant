"""
Background jobs:
  - every hour: check which users' preferred briefing hour just arrived
    (in their own timezone) and send it
  - every ALERT_POLL_INTERVAL_SECONDS: evaluate price-move alerts

The bot instance is injected at startup so jobs can actually push messages,
not just compute them.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.database import get_session
from app.models import User, WatchlistItem, Preference, BriefingLog
from app.services.alert_service import evaluate_alerts
from app.services.briefing_service import build_morning_brief

logger = logging.getLogger("atlas.scheduler")


class AtlasScheduler:
    def __init__(self, send_message_callback):
        """send_message_callback: async fn(telegram_id: int, text: str) -> None"""
        self._send = send_message_callback
        self.scheduler = AsyncIOScheduler()

    def start(self) -> None:
        self.scheduler.add_job(self.run_briefing_check, "interval", minutes=60, next_run_time=dt.datetime.utcnow())
        self.scheduler.add_job(
            self.run_alert_check,
            "interval",
            seconds=settings.alert_poll_interval_seconds,
        )
        self.scheduler.start()
        logger.info("Atlas scheduler started (briefings hourly, alerts every %ss)", settings.alert_poll_interval_seconds)

    async def run_briefing_check(self) -> None:
        current_utc_hour = dt.datetime.utcnow().hour
        with get_session() as db:
            users = db.execute(select(User).where(User.briefing_hour_local.is_not(None))).scalars().all()
            for user in users:
                # Simplified TZ handling: briefing_hour_local is compared against UTC hour
                # offset by a fixed per-user offset stored implicitly in timezone name lookup
                # at send time in a production build; for the hackathon scope we run in UTC
                # and let users set their briefing hour in UTC terms (documented in onboarding).
                if user.briefing_hour_local != current_utc_hour:
                    continue
                watchlist = db.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id)).scalars().all()
                prefs = db.execute(select(Preference).where(Preference.user_id == user.id)).scalars().all()
                brief = build_morning_brief(user, watchlist, prefs)
                if not brief:
                    continue
                db.add(BriefingLog(user_id=user.id, kind="morning_brief", content=brief))
                await self._send(user.telegram_id, f"☀️ **Morning Brief**\n\n{brief}")

    async def run_alert_check(self) -> None:
        with get_session() as db:
            triggered = evaluate_alerts(db)
            users_by_id = {}
            for alert, message in triggered:
                user = users_by_id.get(alert.user_id)
                if not user:
                    user = db.get(User, alert.user_id)
                    users_by_id[alert.user_id] = user
                if user:
                    db.add(BriefingLog(user_id=user.id, kind="alert", content=message))
                    await self._send(user.telegram_id, message)
