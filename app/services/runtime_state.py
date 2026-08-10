"""Small process-local health registry shared by scheduler and API."""
from __future__ import annotations

import datetime as dt
import threading
from dataclasses import dataclass


@dataclass
class RuntimeState:
    scheduler_running: bool = False
    last_alert_check: dt.datetime | None = None
    last_briefing_check: dt.datetime | None = None


runtime_state = RuntimeState()


class ReliabilityTelemetry:
    """Process-local aggregate counters; never stores errors, prompts, or secrets."""

    _NAMES = {
        "primary_provider_failed", "fallback_provider_used", "llm_primary_failed",
        "llm_secondary_used", "deterministic_fallback_used", "cached_financial_data_used",
        "cache_hits", "cache_misses", "requests_coalesced", "provider_429_count",
        "circuit_breaker_skips",
    }

    def __init__(self) -> None:
        self._counts = {name: 0 for name in self._NAMES}
        self._upstream_calls: dict[str, int] = {}
        self._lock = threading.Lock()

    def increment(self, name: str) -> None:
        if name not in self._NAMES:
            return
        with self._lock:
            self._counts[name] += 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {**self._counts, "upstream_calls_by_provider": dict(self._upstream_calls)}

    def record_upstream_call(self, provider: str) -> None:
        safe_name = str(provider or "unknown").strip().lower()[:64]
        with self._lock:
            self._upstream_calls[safe_name] = self._upstream_calls.get(safe_name, 0) + 1


reliability_telemetry = ReliabilityTelemetry()
