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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from app.config import settings
from app.database import get_session
from app.models import User, WatchlistItem, Preference, BriefingLog
from app.services.alert_service import evaluate_alerts
from app.services.briefing_service import build_morning_brief
from app.services.runtime_state import runtime_state

logger = logging.getLogger("atlas.scheduler")


class AtlasScheduler:
    def __init__(self, send_message_callback):
        """send_message_callback: async fn(telegram_id: int, text: str) -> None"""
        self._send = send_message_callback
        self.scheduler = AsyncIOScheduler(
            timezone=ZoneInfo(settings.default_timezone),
            # Coalesce laptop sleep/wake gaps into one run. Jobs re-check current
            # time/data, so executing once after wake is safer than noisy misfires.
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": None},
        )
        self.running = False
        self.last_alert_check: dt.datetime | None = None
        self.last_briefing_check: dt.datetime | None = None

    def start(self) -> None:
        now = dt.datetime.now(ZoneInfo(settings.default_timezone))
        self.scheduler.add_job(self.run_briefing_check, "interval", minutes=60, next_run_time=now)
        self.scheduler.add_job(
            self.run_alert_check,
            "interval",
            seconds=settings.alert_poll_interval_seconds,
        )
        self.scheduler.start()
        self.running = True
        runtime_state.scheduler_running = True
        logger.info("Atlas scheduler started (briefings hourly, alerts every %ss)", settings.alert_poll_interval_seconds)

    async def run_briefing_check(self) -> None:
        self.last_briefing_check = dt.datetime.utcnow()
        runtime_state.last_briefing_check = self.last_briefing_check
        try:
            messages = await asyncio.to_thread(self._collect_briefings)
        except Exception:
            logger.exception("Briefing check failed without stopping the scheduler")
            return
        for telegram_id, message in messages:
            await self._send(telegram_id, message)

    def _collect_briefings(self) -> list[tuple[int, str]]:
        messages: list[tuple[int, str]] = []
        with get_session() as db:
            users = db.execute(select(User).where(User.briefing_hour_local.is_not(None))).scalars().all()
            for user in users:
                try:
                    local_hour = dt.datetime.now(ZoneInfo(user.timezone or settings.default_timezone)).hour
                except ZoneInfoNotFoundError:
                    local_hour = dt.datetime.utcnow().hour
                if user.briefing_hour_local != local_hour:
                    continue
                recent_cutoff = dt.datetime.utcnow() - dt.timedelta(hours=20)
                already_sent = db.execute(select(BriefingLog).where(
                    BriefingLog.user_id == user.id,
                    BriefingLog.kind == "morning_brief",
                    BriefingLog.created_at >= recent_cutoff,
                )).scalar_one_or_none()
                if already_sent:
                    continue
                watchlist = db.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id)).scalars().all()
                prefs = db.execute(select(Preference).where(Preference.user_id == user.id)).scalars().all()
                brief = build_morning_brief(user, watchlist, prefs)
                if not brief:
                    continue
                db.add(BriefingLog(user_id=user.id, kind="morning_brief", content=brief))
                messages.append((user.telegram_id, f"☀️ **Morning Brief**\n\n{brief}"))
        return messages

    async def run_alert_check(self) -> None:
        self.last_alert_check = dt.datetime.utcnow()
        runtime_state.last_alert_check = self.last_alert_check
        try:
            messages = await asyncio.to_thread(self._collect_alerts)
        except Exception:
            logger.exception("Alert check failed without stopping the scheduler")
            return
        for telegram_id, message in messages:
            await self._send(telegram_id, message)

    def _collect_alerts(self) -> list[tuple[int, str]]:
        messages: list[tuple[int, str]] = []
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
                    messages.append((user.telegram_id, message))
        return messages
