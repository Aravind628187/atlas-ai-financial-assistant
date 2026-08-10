"""Explicit provider-unit normalization and conservative sanity validation."""
from __future__ import annotations

import math
from typing import Any, Literal


RatioUnit = Literal["fraction", "percent"]


def number(value: Any, *, minimum: float | None = None,
           maximum: float | None = None) -> float | None:
    if value in (None, "", "None", "null", "-"):
        return None
    try:
        parsed = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    if maximum is not None and parsed > maximum:
        return None
    return parsed


def ratio(value: Any, *, unit: RatioUnit, minimum: float = -10.0,
          maximum: float = 10.0) -> float | None:
    """Normalize to a fraction: 0.0045 always means 0.45% inside Atlas."""
    parsed = number(value)
    if parsed is None:
        return None
    normalized = parsed / 100.0 if unit == "percent" else parsed
    return normalized if minimum <= normalized <= maximum else None


def positive_money(value: Any) -> float | None:
    return number(value, minimum=0, maximum=1e18)


def multiple(value: Any) -> float | None:
    return number(value, minimum=-10000, maximum=10000)


def epoch_datetime(value: Any, *, divisor: float = 1.0):
    import datetime as dt
    parsed = number(value, minimum=0)
    if parsed is None:
        return None
    try:
        return dt.datetime.fromtimestamp(parsed / divisor, tz=dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
