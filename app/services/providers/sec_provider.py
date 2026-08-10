"""Official SEC EDGAR submissions provider with conservative request pacing."""
from __future__ import annotations

import datetime as dt
import threading
import time
from typing import Any

import httpx

from app.config import settings
from app.services.providers.base import DataStatus, FilingItem, FilingsData, FinancialDataResult


UTC = dt.timezone.utc


class SECProvider:
    name = "SEC EDGAR"
    _lock = threading.Lock()
    _last_request = 0.0
    _ticker_cache: tuple[float, dict[str, tuple[str, str]]] | None = None

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=settings.provider_timeout_seconds,
            headers={"User-Agent": settings.sec_user_agent, "Accept-Encoding": "gzip, deflate"},
            follow_redirects=True,
        )

    @classmethod
    def _pace(cls) -> None:
        with cls._lock:
            remaining = 0.12 - (time.monotonic() - cls._last_request)
            if remaining > 0:
                time.sleep(remaining)
            cls._last_request = time.monotonic()

    def _get_json(self, url: str) -> Any:
        self._pace()
        response = self.client.get(url)
        response.raise_for_status()
        return response.json()

    def _tickers(self) -> dict[str, tuple[str, str]]:
        now = time.monotonic()
        cached = type(self)._ticker_cache
        if cached and cached[0] > now:
            return cached[1]
        payload = self._get_json("https://www.sec.gov/files/company_tickers.json")
        mapping = {
            str(row["ticker"]).upper(): (str(row["cik_str"]).zfill(10), str(row["title"]))
            for row in payload.values()
            if isinstance(row, dict) and row.get("ticker") and row.get("cik_str")
        }
        type(self)._ticker_cache = (now + 86400, mapping)
        return mapping

    @staticmethod
    def _date(value: str | None) -> dt.date | None:
        try:
            return dt.date.fromisoformat(value or "")
        except ValueError:
            return None

    def get_filings(self, symbol: str, forms: tuple[str, ...] = ("10-Q", "10-K", "8-K"), limit: int = 5) -> FinancialDataResult:
        clean = symbol.upper()
        now = dt.datetime.now(UTC)
        try:
            identity = self._tickers().get(clean)
            if not identity:
                raise LookupError("ticker is not in the SEC company ticker index")
            cik, indexed_name = identity
            payload = self._get_json(f"https://data.sec.gov/submissions/CIK{cik}.json")
            recent = payload.get("filings", {}).get("recent", {})
            rows: list[FilingItem] = []
            count = len(recent.get("form", []))
            for index in range(count):
                form = recent["form"][index]
                if form not in forms:
                    continue
                filed = self._date(recent.get("filingDate", [None] * count)[index])
                if not filed:
                    continue
                accession = recent.get("accessionNumber", [None] * count)[index]
                primary = recent.get("primaryDocument", [None] * count)[index]
                accession_path = str(accession or "").replace("-", "")
                url = (
                    f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession_path}/{primary}"
                    if accession_path and primary else None
                )
                rows.append(FilingItem(
                    symbol=clean, company_name=payload.get("name") or indexed_name, form=form,
                    filed_at=filed, report_date=self._date(recent.get("reportDate", [None] * count)[index]),
                    accession_number=accession, primary_document=primary, url=url,
                ))
                if len(rows) >= max(1, min(limit, 10)):
                    break
            data = FilingsData(clean, cik, payload.get("name") or indexed_name, rows)
            return FinancialDataResult(
                status=DataStatus.OK if rows else DataStatus.UNAVAILABLE,
                source=self.name, source_type="regulatory_filing", retrieved_at=now,
                data_as_of=dt.datetime.combine(rows[0].filed_at, dt.time(), UTC) if rows else None,
                symbol=clean, data=data if rows else None, is_realtime=False, is_delayed=False,
                error=None if rows else "no matching recent filings",
            )
        except Exception as exc:
            return FinancialDataResult(
                status=DataStatus.UNAVAILABLE, source=self.name, source_type="regulatory_filing",
                retrieved_at=now, data_as_of=None, symbol=clean, data=None,
                is_realtime=False, is_delayed=False, error=type(exc).__name__,
            )
