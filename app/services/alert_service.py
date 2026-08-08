"""
Price-move alert evaluation. Kept separate from the scheduler so it can be
unit-tested without spinning up APScheduler.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Alert
from app.services.market_data import get_quote

logger = logging.getLogger("atlas.alerts")

DEFAULT_THRESHOLD_PCT = 5.0


def evaluate_alerts(db: Session) -> list[tuple[Alert, str]]:
    """Returns a list of (Alert, message) for every alert that should fire right now."""
    triggered: list[tuple[Alert, str]] = []
    alerts = db.execute(select(Alert).where(Alert.active.is_(True))).scalars().all()

    for alert in alerts:
        quote = get_quote(alert.symbol)
        if not quote or quote.change_pct is None:
            continue

        threshold = alert.threshold_pct or DEFAULT_THRESHOLD_PCT
        if abs(quote.change_pct) < threshold:
            continue

        # Avoid re-firing on the same move repeatedly within a day.
        if alert.last_triggered_at and alert.last_triggered_at.date() == dt.datetime.utcnow().date():
            if alert.last_triggered_price and abs(quote.price - alert.last_triggered_price) < (quote.price * 0.005):
                continue

        direction_word = "up 📈" if quote.change_pct > 0 else "down 📉"
        message = (
            f"⚡ **{alert.symbol}** just moved **{quote.change_pct:+.2f}%** {direction_word} "
            f"to **{quote.price} {quote.currency or ''}**. Crossed your {threshold:.0f}% alert threshold."
        )
        alert.last_triggered_price = quote.price
        alert.last_triggered_at = dt.datetime.utcnow()
        triggered.append((alert, message))

    db.flush()
    return triggered
