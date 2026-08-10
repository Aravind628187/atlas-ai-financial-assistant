"""Freshness and US market-session semantics for financial data."""
from __future__ import annotations

import datetime as dt
from enum import Enum
from zoneinfo import ZoneInfo


UTC = dt.timezone.utc
NEW_YORK = ZoneInfo("America/New_York")


class Freshness(str, Enum):
    LIVE = "live"
    DELAYED = "delayed"
    LATEST_AVAILABLE = "latest_available"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


class MarketSession(str, Enum):
    PRE_MARKET = "pre_market"
    OPEN = "open"
    AFTER_HOURS = "after_hours"
    CLOSED = "closed"


def ensure_utc(value: dt.datetime | None) -> dt.datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def market_session_status(now: dt.datetime | None = None) -> MarketSession:
    """Conservative weekday/session check; providers remain authoritative on holidays."""
    local = (ensure_utc(now) or dt.datetime.now(UTC)).astimezone(NEW_YORK)
    if local.weekday() >= 5:
        return MarketSession.CLOSED
    current = local.time().replace(tzinfo=None)
    if dt.time(4, 0) <= current < dt.time(9, 30):
        return MarketSession.PRE_MARKET
    if dt.time(9, 30) <= current < dt.time(16, 0):
        return MarketSession.OPEN
    if dt.time(16, 0) <= current < dt.time(20, 0):
        return MarketSession.AFTER_HOURS
    return MarketSession.CLOSED


def quote_freshness(
    data_as_of: dt.datetime | None,
    *,
    is_realtime: bool = False,
    is_delayed: bool = True,
    stale_after_seconds: int = 900,
    now: dt.datetime | None = None,
) -> Freshness:
    as_of = ensure_utc(data_as_of)
    current = ensure_utc(now) or dt.datetime.now(UTC)
    if as_of is None:
        return Freshness.UNAVAILABLE
    age = max(0.0, (current - as_of).total_seconds())
    session = market_session_status(current)
    if age > stale_after_seconds and session == MarketSession.OPEN:
        return Freshness.STALE
    if session != MarketSession.OPEN:
        return Freshness.LATEST_AVAILABLE
    if is_realtime and age <= 60:
        return Freshness.LIVE
    return Freshness.DELAYED if is_delayed else Freshness.LATEST_AVAILABLE


def is_news_fresh(published_at: dt.datetime | None, max_age_hours: int = 48) -> bool:
    value = ensure_utc(published_at)
    return bool(value and dt.datetime.now(UTC) - value <= dt.timedelta(hours=max_age_hours))


def is_fundamental_current(data_as_of: dt.datetime | None, max_age_days: int = 120) -> bool:
    value = ensure_utc(data_as_of)
    return bool(value and dt.datetime.now(UTC) - value <= dt.timedelta(days=max_age_days))
