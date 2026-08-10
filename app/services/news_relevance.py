"""Deterministic company-news relevance ranking and near-duplicate removal."""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import urlsplit, urlunsplit

from app.services.entity_resolution import KNOWN_COMPANIES
from app.services.providers.base import NewsItem


def _normalized_title(value: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", value.lower()).strip()


def _canonical_url(value: str | None) -> str:
    if not value:
        return ""
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def _aliases(symbol: str) -> set[str]:
    aliases = {symbol.lower()}
    aliases.update(name for name, ticker in KNOWN_COMPANIES.items() if ticker == symbol.upper())
    return aliases


def relevance_score(item: NewsItem, symbol: str) -> int:
    title = (item.headline or "").lower()
    body = (item.summary or "").lower()
    aliases = _aliases(symbol)
    score = 0
    headline_match = False
    body_match = False
    for alias in aliases:
        token = rf"\b{re.escape(alias)}\b"
        if re.search(token, title):
            headline_match = True
            score = max(score, 10 if alias == symbol.lower() else 9)
        if re.search(token, body):
            body_match = True
            score = max(score, 3)
    low_value = ("etf", "crypto", "bitcoin", "price prediction", "top stocks", "stocks to buy",
                 "stock list", "weekly roundup")
    if any(term in title for term in low_value):
        score -= 8
    # A competitor-centric headline that merely mentions the target company
    # in its body is related context, not company news.
    competitor_names = {name for name, ticker in KNOWN_COMPANIES.items() if ticker != symbol.upper()}
    if not headline_match and any(re.search(rf"\b{re.escape(name)}\b", title) for name in competitor_names):
        score -= 6
    direct_impact = ("supplier", "customer", "regulator", "antitrust", "export", "deal")
    impact_language = ("directly affects", "material impact", "halts shipments to", "deal with")
    if not headline_match and body_match and any(term in title for term in direct_impact) and any(term in body for term in impact_language):
        score += 4
    if item.published_at: score += 1
    if item.publisher: score += 1
    return score


def filter_company_news(items: list[NewsItem], symbol: str, limit: int = 5) -> list[NewsItem]:
    ranked = sorted(((relevance_score(item, symbol), item) for item in items), key=lambda pair: pair[0], reverse=True)
    output: list[NewsItem] = []
    titles: list[str] = []
    urls: set[str] = set()
    for score, item in ranked:
        if score < 8:
            continue
        title = _normalized_title(item.headline)
        url = _canonical_url(item.url)
        if url and url in urls:
            continue
        if any(SequenceMatcher(None, title, previous).ratio() >= 0.86 for previous in titles):
            continue
        output.append(item)
        titles.append(title)
        if url: urls.add(url)
        if len(output) >= limit:
            break
    return output


def is_verified_catalyst(item: NewsItem, symbol: str) -> bool:
    """True only when the retrieved wording explicitly links news to a share move."""
    text = f"{item.headline or ''} {item.summary or ''}".lower()
    if relevance_score(item, symbol) < 8:
        return False
    causal_phrases = (
        "shares rise after", "shares fall after", "stock rises after", "stock falls after",
        "shares jump after", "shares drop after", "stock jumps after", "stock drops after",
        "shares gain on", "shares slide on", "stock gains on", "stock slides on",
        "drives shares", "sends shares", "weighs on shares",
    )
    return any(phrase in text for phrase in causal_phrases)
