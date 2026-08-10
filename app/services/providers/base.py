"""Provider contracts and normalized financial schemas."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Protocol


class DataStatus(str, Enum):
    OK = "ok"
    DELAYED = "delayed"
    STALE = "stale"
    UNAVAILABLE = "unavailable"
    PARTIAL = "partial"
    NOT_CONFIGURED = "not_configured"
    INVALID_CREDENTIALS = "invalid_credentials"
    RATE_LIMITED = "rate_limited"
    NOT_ENTITLED = "not_entitled"
    DEGRADED = "degraded"
    ERROR = "error"
    CONFLICTING_DATA = "conflicting_data"


@dataclass(slots=True)
class QuoteData:
    symbol: str
    price: float | None = None
    previous_close: float | None = None
    change: float | None = None
    change_pct: float | None = None
    currency: str | None = None
    name: str | None = None
    market_state: str | None = None


@dataclass(slots=True)
class CompanyProfile:
    symbol: str
    name: str | None = None
    sector: str | None = None
    industry: str | None = None
    summary: str | None = None
    website: str | None = None


@dataclass(slots=True)
class FundamentalsData:
    symbol: str
    market_cap: float | None = None
    trailing_pe: float | None = None
    forward_pe: float | None = None
    revenue: float | None = None
    profit_margin: float | None = None
    revenue_growth: float | None = None
    fifty_two_week_high: float | None = None
    fifty_two_week_low: float | None = None
    dividend_yield: float | None = None
    revenue_period: str | None = None
    margin_period: str | None = None
    unit_notes: dict[str, str] = field(default_factory=dict)
    dividend_yield_ttm: float | None = None
    dividend_yield_forward: float | None = None
    dividend_yield_indicated: float | None = None
    revenue_growth_period: str | None = None
    currency: str | None = None
    metric_definitions: dict[str, str] = field(default_factory=dict)


FUNDAMENTAL_VALUE_FIELDS = (
    "market_cap", "trailing_pe", "forward_pe", "revenue", "profit_margin",
    "revenue_growth", "fifty_two_week_high", "fifty_two_week_low", "dividend_yield",
)


def has_fundamental_values(data: FundamentalsData) -> bool:
    return any(getattr(data, name) is not None for name in FUNDAMENTAL_VALUE_FIELDS)


@dataclass(slots=True)
class NewsItem:
    news_id: str
    headline: str
    publisher: str | None = None
    published_at: dt.datetime | None = None
    url: str | None = None
    summary: str | None = None


@dataclass(slots=True)
class EarningsData:
    symbol: str
    next_earnings_at: dt.datetime | None = None
    period: str | None = None
    status: str = "unverified"
    source: str | None = None
    verified_with: str | None = None
    earnings_date: dt.datetime | None = None

    def __post_init__(self) -> None:
        if self.earnings_date is None:
            self.earnings_date = self.next_earnings_at
        elif self.next_earnings_at is None:
            self.next_earnings_at = self.earnings_date
        if self.status not in {"confirmed", "estimated", "unverified"}:
            self.status = "unverified"


def normalize_earnings_datetime(value: Any) -> dt.datetime | None:
    """Normalize provider date/date-time values to UTC without inventing a time."""
    if value is None or value == "":
        return None
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=value.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time(), tzinfo=dt.timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return dt.datetime.fromtimestamp(value, tz=dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).astimezone(dt.timezone.utc)


def earliest_future_earnings(rows: list[dict[str, Any]], date_keys: tuple[str, ...],
                             today: dt.date | None = None) -> tuple[dt.datetime | None, dict[str, Any] | None]:
    """Return the earliest valid provider record strictly after today."""
    current = today or dt.datetime.now(dt.timezone.utc).date()
    candidates: list[tuple[dt.datetime, dict[str, Any]]] = []
    for row in rows:
        value = next((row.get(key) for key in date_keys if row.get(key)), None)
        normalized = normalize_earnings_datetime(value)
        if normalized and normalized.date() > current:
            candidates.append((normalized, row))
    return min(candidates, key=lambda item: item[0]) if candidates else (None, None)


@dataclass(slots=True)
class MarketStatus:
    exchange: str | None = None
    session: str = "closed"
    timezone: str = "America/New_York"


@dataclass(slots=True)
class HistoricalPoint:
    timestamp: dt.datetime
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    volume: int | None = None


@dataclass(slots=True)
class HistoricalData:
    symbol: str
    points: list[HistoricalPoint] = field(default_factory=list)


@dataclass(slots=True)
class FilingItem:
    symbol: str
    company_name: str
    form: str
    filed_at: dt.date
    report_date: dt.date | None = None
    accession_number: str | None = None
    primary_document: str | None = None
    url: str | None = None


@dataclass(slots=True)
class FilingsData:
    symbol: str
    cik: str
    company_name: str
    filings: list[FilingItem] = field(default_factory=list)


@dataclass(slots=True)
class FinancialDataResult:
    status: DataStatus
    source: str
    source_type: str
    retrieved_at: dt.datetime
    data_as_of: dt.datetime | None
    symbol: str | None
    data: Any = None
    is_realtime: bool = False
    is_delayed: bool = True
    is_stale: bool = False
    cache_hit: bool = False
    error: str | None = None
    verification: dict[str, Any] | None = None
    freshness: str | None = None
    market_status: str | None = None
    # A provider quote timestamp and a daily-bar session label are different
    # concepts.  Keeping them explicit prevents midnight/session-open labels
    # from being presented as final-trade timestamps.
    timestamp_kind: str | None = None
    data_date: dt.date | None = None
    interval: str | None = None
    exchange_timezone: str | None = None

    @property
    def provider(self) -> str:
        return self.source

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["status"] = self.status.value
        value["provider"] = self.source
        return value


class MarketDataProvider(Protocol):
    name: str

    def get_quote(self, symbol: str) -> FinancialDataResult: ...
    def get_profile(self, symbol: str) -> FinancialDataResult: ...
    def get_fundamentals(self, symbol: str) -> FinancialDataResult: ...
    def get_news(self, symbol: str, limit: int = 5) -> FinancialDataResult: ...
    def get_earnings(self, symbol: str) -> FinancialDataResult: ...
    def get_history(self, symbol: str, period: str) -> FinancialDataResult: ...


def unavailable_result(provider: str, symbol: str, operation: str, error: str,
                       status: DataStatus = DataStatus.UNAVAILABLE) -> FinancialDataResult:
    return FinancialDataResult(
        status=status, source=provider, source_type=operation,
        retrieved_at=dt.datetime.now(dt.timezone.utc), data_as_of=None,
        symbol=symbol.upper(), data=None, is_realtime=False, is_delayed=False,
        error=error,
    )
