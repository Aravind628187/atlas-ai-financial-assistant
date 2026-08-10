"""Conservative company-name and ticker resolution."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


KNOWN_COMPANIES = {
    "nvidia": "NVDA", "microsoft": "MSFT", "apple": "AAPL", "amazon": "AMZN",
    "alphabet": "GOOGL", "google": "GOOGL", "tesla": "TSLA", "meta": "META",
    "facebook": "META", "amd": "AMD", "advanced micro devices": "AMD",
    "netflix": "NFLX", "broadcom": "AVGO", "intel": "INTC", "oracle": "ORCL",
    "spacex": "SPCX", "berkshire hathaway": "BRK-B", "berkshire": "BRK-B",
    "eli lilly": "LLY", "micron technology": "MU", "micron": "MU",
    "jpmorgan chase": "JPM", "jpmorgan": "JPM", "walmart": "WMT",
}
TICKER_TOKEN = re.compile(r"\b[A-Z]{1,5}(?:\.[A-Z])?\b")
STOPWORDS = {"I", "A", "AN", "THE", "AND", "OR", "PE", "P", "CAGR", "ETF", "AI", "USD"}


@dataclass(slots=True)
class EntityResolution:
    symbols: list[str] = field(default_factory=list)
    ambiguous: bool = False
    clarification: str = ""


def resolve_entities(text: str, hinted: list[str] | None = None) -> EntityResolution:
    lowered = text.lower()
    symbols: list[str] = []
    for name, symbol in sorted(KNOWN_COMPANIES.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(name)}\b", lowered) and symbol not in symbols:
            symbols.append(symbol)
    for token in TICKER_TOKEN.findall(text):
        if token not in STOPWORDS and token not in symbols:
            symbols.append(token)
    for token in hinted or []:
        clean = str(token).strip().upper()
        if re.fullmatch(r"[A-Z]{1,5}(?:\.[A-Z])?", clean) and clean not in STOPWORDS and clean not in symbols:
            symbols.append(clean)
    return EntityResolution(symbols=symbols[:10])
