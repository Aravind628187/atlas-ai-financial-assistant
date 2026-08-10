"""Small process-local health registry shared by scheduler and API."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class RuntimeState:
    scheduler_running: bool = False
    last_alert_check: dt.datetime | None = None
    last_briefing_check: dt.datetime | None = None


runtime_state = RuntimeState()
