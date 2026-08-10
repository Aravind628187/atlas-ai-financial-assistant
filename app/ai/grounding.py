"""Strict schema passed to AI synthesis."""
from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(slots=True)
class GroundingContext:
    intent: str
    symbols: list[str] = field(default_factory=list)
    verified_numbers: list[float] = field(default_factory=list)
    verified_facts: dict[str, Any] = field(default_factory=dict)
    news: list[dict[str, Any]] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    data_as_of: list[str] = field(default_factory=list)
    freshness: str = "unavailable"
    limitations: list[str] = field(default_factory=list)

    def add_number(self, value: Any) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            self.verified_numbers.append(float(value))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
