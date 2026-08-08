"""
Read-only analytics API behind the Atlas dashboard.

The dashboard reads data from the same database written by the Telegram bot.
It does not communicate directly with Telegram or Gemini.
"""

from __future__ import annotations

import datetime as dt
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select

from app.database import get_session, init_db
from app.models import (
    Alert,
    BriefingLog,
    Document,
    Message,
    User,
    WatchlistItem,
)


# ============================================================
# Dashboard directory
# ============================================================

DASHBOARD_DIR = os.path.join(
    os.path.dirname(
        os.path.dirname(
            os.path.dirname(__file__)
        )
    ),
    "dashboard",
)


# ============================================================
# Helpers
# ============================================================

def utc_iso(value: dt.datetime | None) -> str | None:
    """
    Convert database timestamps to ISO-8601 UTC strings.

    The database currently stores naive UTC timestamps.
    Adding Z tells JavaScript that the timestamp is UTC so
    the browser can correctly convert it to the user's local timezone.
    """

    if value is None:
        return None

    # Already timezone-aware
    if value.tzinfo is not None:
        return value.astimezone(
            dt.timezone.utc
        ).isoformat().replace("+00:00", "Z")

    # Naive datetime is treated as UTC
    return value.isoformat() + "Z"


# ============================================================
# FastAPI application
# ============================================================

def create_dashboard_app() -> FastAPI:
    init_db()

    app = FastAPI(
        title="Atlas AI Dashboard API"
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )


    # ========================================================
    # Overview
    # ========================================================

    @app.get("/api/overview")
    def overview():
        with get_session() as db:

            total_users = (
                db.scalar(
                    select(func.count(User.id))
                )
                or 0
            )

            done_users = (
                db.scalar(
                    select(func.count(User.id)).where(
                        User.onboarding_stage == "done"
                    )
                )
                or 0
            )

            since = (
                dt.datetime.utcnow()
                - dt.timedelta(hours=24)
            )

            messages_24h = (
                db.scalar(
                    select(func.count(Message.id)).where(
                        Message.created_at >= since
                    )
                )
                or 0
            )

            total_watchlist = (
                db.scalar(
                    select(
                        func.count(WatchlistItem.id)
                    )
                )
                or 0
            )

            active_alerts = (
                db.scalar(
                    select(func.count(Alert.id)).where(
                        Alert.active.is_(True)
                    )
                )
                or 0
            )

            documents_processed = (
                db.scalar(
                    select(func.count(Document.id))
                )
                or 0
            )

            briefings_sent = (
                db.scalar(
                    select(
                        func.count(BriefingLog.id)
                    )
                )
                or 0
            )

            onboarding_pct = (
                round(
                    (done_users / total_users) * 100,
                    1,
                )
                if total_users
                else 0
            )

            return {
                "total_users": total_users,
                "onboarded_users": done_users,
                "onboarding_completion_pct": onboarding_pct,
                "messages_last_24h": messages_24h,
                "watchlist_items": total_watchlist,
                "active_alerts": active_alerts,
                "documents_processed": documents_processed,
                "briefings_sent": briefings_sent,
            }


    # ========================================================
    # Popular symbols
    # ========================================================

    @app.get("/api/symbols/popular")
    def popular_symbols(limit: int = 10):
        with get_session() as db:

            rows = db.execute(
                select(
                    WatchlistItem.symbol,
                    func.count(
                        WatchlistItem.id
                    ).label("count"),
                )
                .group_by(
                    WatchlistItem.symbol
                )
                .order_by(
                    func.count(
                        WatchlistItem.id
                    ).desc()
                )
                .limit(limit)
            ).all()

            return [
                {
                    "symbol": row[0],
                    "count": row[1],
                }
                for row in rows
            ]


    # ========================================================
    # Message volume
    # ========================================================

    @app.get("/api/messages/volume")
    def message_volume(days: int = 7):
        with get_session() as db:

            # Keep a sensible range
            days = max(
                1,
                min(days, 365),
            )

            since = (
                dt.datetime.utcnow()
                - dt.timedelta(days=days)
            )

            rows = db.execute(
                select(
                    Message.created_at
                ).where(
                    Message.created_at >= since
                )
            ).all()

            buckets: dict[str, int] = {}

            for (created_at,) in rows:

                if not created_at:
                    continue

                key = created_at.strftime(
                    "%Y-%m-%d"
                )

                buckets[key] = (
                    buckets.get(key, 0) + 1
                )

            ordered = sorted(
                buckets.items()
            )

            return [
                {
                    "date": date,
                    "count": count,
                }
                for date, count in ordered
            ]


    # ========================================================
    # Users
    # ========================================================

    @app.get("/api/users")
    def list_users(limit: int = 50):
        with get_session() as db:

            limit = max(
                1,
                min(limit, 200),
            )

            users = (
                db.execute(
                    select(User)
                    .order_by(
                        User.last_active_at.desc()
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            output = []

            for user in users:

                message_count = (
                    db.scalar(
                        select(
                            func.count(Message.id)
                        ).where(
                            Message.user_id
                            == user.id
                        )
                    )
                    or 0
                )

                output.append(
                    {
                        "id": user.id,
                        "username": user.username,
                        "first_name": user.first_name,
                        "role": user.role,
                        "onboarding_stage":
                            user.onboarding_stage,
                        "message_count":
                            message_count,
                        "watchlist_size":
                            len(
                                user.watchlist_items
                            ),
                        "last_active_at":
                            utc_iso(
                                user.last_active_at
                            ),
                        "created_at":
                            utc_iso(
                                user.created_at
                            ),
                    }
                )

            return output


    # ========================================================
    # Recent messages
    # ========================================================

    @app.get("/api/messages/recent")
    def recent_messages(limit: int = 30):
        with get_session() as db:

            limit = max(
                1,
                min(limit, 200),
            )

            rows = (
                db.execute(
                    select(Message)
                    .order_by(
                        Message.created_at.desc()
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return [
                {
                    "id": message.id,
                    "user_id":
                        message.user_id,
                    "role":
                        message.role,
                    "content":
                        (
                            message.content
                            or ""
                        )[:280],
                    "intent":
                        message.intent,
                    "input_kind":
                        message.input_kind,
                    "created_at":
                        utc_iso(
                            message.created_at
                        ),
                }
                for message in rows
            ]


    # ========================================================
    # Recent briefings
    # ========================================================

    @app.get("/api/briefings/recent")
    def recent_briefings(limit: int = 20):
        with get_session() as db:

            limit = max(
                1,
                min(limit, 100),
            )

            rows = (
                db.execute(
                    select(BriefingLog)
                    .order_by(
                        BriefingLog.created_at.desc()
                    )
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            return [
                {
                    "user_id":
                        briefing.user_id,
                    "kind":
                        briefing.kind,
                    "content":
                        briefing.content,
                    "created_at":
                        utc_iso(
                            briefing.created_at
                        ),
                }
                for briefing in rows
            ]


    # ========================================================
    # Health check
    # ========================================================

    @app.get("/api/health")
    def health():
        return {
            "status": "ok",
            "service": "atlas-ai",
        }


    # ========================================================
    # Dashboard static files
    # IMPORTANT: Keep this LAST.
    # ========================================================

    if os.path.isdir(
        DASHBOARD_DIR
    ):
        app.mount(
            "/",
            StaticFiles(
                directory=DASHBOARD_DIR,
                html=True,
            ),
            name="dashboard",
        )

    return app