#!/usr/bin/env python
"""
Populates the database with realistic-looking demo data so the dashboard
has something to show immediately — useful for screenshots/demo videos
before you've chatted with the real bot. Safe to run multiple times.

Usage:  python scripts/seed_demo_data.py
"""
import datetime as dt
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import get_session, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Alert,
    BriefingLog,
    Message,
    Preference,
    User,
    WatchlistItem,
)

DEMO_USERS = [
    {"telegram_id": 1001, "username": "priya_invests", "first_name": "Priya", "role": "Investor",
     "watchlist": ["AAPL", "MSFT", "NVDA"]},
    {"telegram_id": 1002, "username": "arjun_analyst", "first_name": "Arjun", "role": "Analyst",
     "watchlist": ["TSLA", "AMZN"]},
    {"telegram_id": 1003, "username": "founder_mia", "first_name": "Mia", "role": "Founder",
     "watchlist": ["GOOGL", "META", "NFLX"]},
    {"telegram_id": 1004, "username": "quant_dev", "first_name": "Dev", "role": "Finance Professional",
     "watchlist": ["JPM", "GS"]},
    {"telegram_id": 1005, "username": "student_kavya", "first_name": "Kavya", "role": "Student",
     "watchlist": ["AAPL"]},
]

SAMPLE_TURNS = [
    ("What's moving Nvidia today?", "market_data"),
    ("Compare Microsoft and Google from an investment perspective.", "company_research"),
    ("Track Tesla and notify me on major moves.", "alert_create"),
    ("Summarize Apple's latest earnings call in five points.", "earnings"),
    ("Any big news in semiconductors this week?", "news"),
    ("What's on my watchlist?", "watchlist_view"),
]


def run() -> None:
    init_db()
    with get_session() as db:
        for spec in DEMO_USERS:
            existing = db.query(User).filter_by(telegram_id=spec["telegram_id"]).one_or_none()
            if existing:
                continue
            user = User(
                telegram_id=spec["telegram_id"],
                username=spec["username"],
                first_name=spec["first_name"],
                role=spec["role"],
                onboarding_stage="done",
                briefing_hour_local=random.choice([6, 7, 8, 13]),
                created_at=dt.datetime.utcnow() - dt.timedelta(days=random.randint(1, 30)),
                last_active_at=dt.datetime.utcnow() - dt.timedelta(hours=random.randint(0, 20)),
            )
            db.add(user)
            db.flush()

            for sym in spec["watchlist"]:
                db.add(WatchlistItem(user_id=user.id, symbol=sym))
            db.add(Preference(user_id=user.id, key="role", value=spec["role"]))
            db.add(Alert(user_id=user.id, symbol=spec["watchlist"][0], threshold_pct=5.0))

            for i in range(random.randint(3, 6)):
                q, intent = random.choice(SAMPLE_TURNS)
                ts = dt.datetime.utcnow() - dt.timedelta(hours=random.randint(0, 72))
                db.add(Message(user_id=user.id, role="user", content=q, intent=intent, created_at=ts))
                db.add(
                    Message(
                        user_id=user.id,
                        role="assistant",
                        content="(sample assistant reply — real replies are generated live by Gemini)",
                        intent=intent,
                        created_at=ts + dt.timedelta(seconds=2),
                    )
                )

            db.add(
                BriefingLog(
                    user_id=user.id,
                    kind="morning_brief",
                    content=f"Sample morning brief for {spec['first_name']} covering {', '.join(spec['watchlist'])}.",
                    created_at=dt.datetime.utcnow() - dt.timedelta(hours=random.randint(1, 40)),
                )
            )

    print("✅ Demo data seeded. Start the dashboard and open http://localhost:8000")


if __name__ == "__main__":
    run()
