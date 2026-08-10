"""Deterministic, fully grounded morning intelligence builder."""
from __future__ import annotations

from app.config import settings
from app.models import Preference, User, WatchlistItem
from app.services.financial_data_gateway import gateway
from app.services.providers.base import DataStatus, EarningsData, NewsItem, QuoteData


def _provider_label(value: str) -> str:
    return {"fmp": "FMP", "finnhub": "Finnhub", "alpha_vantage": "Alpha Vantage"}.get(value.lower(), value)


def build_morning_brief(user: User, watchlist: list[WatchlistItem], preferences: list[Preference]) -> str | None:
    symbols = list(dict.fromkeys(item.symbol for item in watchlist))[:settings.max_watchlist_items]
    if not symbols:
        return None
    snapshots: list[str] = []
    quote_rows: list[tuple[float, str, str, float | None]] = []
    news_lines: list[str] = []
    observations: list[tuple[float, str]] = []
    sources: set[str] = set()
    market_dates: list[str] = []
    news_times: list[str] = []
    upcoming: list[str] = []
    try:
        alert_thresholds = {
            alert.symbol: alert.threshold_pct for alert in user.alerts
            if alert.active and alert.kind == "pct_move" and alert.threshold_pct is not None
        }
    except Exception:
        alert_thresholds = {}
    for symbol in symbols:
        result = gateway.get_quote(symbol, verify=False)
        quote = result.data
        cached_verified = bool((result.verification or {}).get("cached_verified"))
        if isinstance(quote, QuoteData) and result.status != DataStatus.UNAVAILABLE and (result.status != DataStatus.STALE or cached_verified):
            move = f", {quote.change_pct:+.2f}%" if quote.change_pct is not None else ""
            cache_label = " (last verified cache)" if cached_verified else ""
            line = f"• {symbol}: {quote.price:,.2f} {quote.currency or ''}{move}{cache_label}".rstrip()
            quote_rows.append((abs(quote.change_pct or 0), symbol, line, quote.change_pct))
            if quote.change_pct is not None:
                observations.append((abs(quote.change_pct), f"• {symbol}'s latest verified session move was {quote.change_pct:+.2f}%."))
            sources.add(result.source)
            if result.data_as_of:
                market_dates.append(result.data_as_of.date().isoformat())
    ranked_quotes = sorted(quote_rows, key=lambda row: row[0], reverse=True)
    surfaced_symbols = [row[1] for row in ranked_quotes[:5]]
    for _, symbol, _, move_pct in ranked_quotes:
        threshold = alert_thresholds.get(symbol)
        if threshold is not None and move_pct is not None and abs(move_pct) >= threshold and symbol not in surfaced_symbols:
            surfaced_symbols.append(symbol)
    line_by_symbol = {row[1]: row[2] for row in ranked_quotes}
    snapshots = [line_by_symbol[symbol] for symbol in surfaced_symbols]

    for symbol in surfaced_symbols[:5]:
        if len(news_lines) >= 3:
            break
        result = gateway.get_news(symbol, 2)
        sources.add(result.source) if result.source != "none" else None
        for item in (result.data or []):
            if isinstance(item, NewsItem):
                published = item.published_at.strftime("%Y-%m-%d %H:%M UTC") if item.published_at else "time unavailable"
                news_lines.append(f"• {symbol}: {item.headline}\n  {item.publisher or result.source} · {published}")
                if item.published_at:
                    news_times.append(item.published_at.strftime("%Y-%m-%d %H:%M UTC"))
    for symbol in surfaced_symbols[:5]:
        if len(upcoming) >= 3:
            break
        result = gateway.get_earnings(symbol)
        event = result.data
        if isinstance(event, EarningsData) and event.earnings_date and event.status in {"confirmed", "estimated"}:
            label = "Estimated earnings" if event.status == "estimated" else "Earnings"
            source = _provider_label(event.source or result.source)
            if event.verified_with:
                corroboration = "estimate corroborated by" if event.status == "estimated" else "verified with"
                verification = f"; {corroboration} {_provider_label(event.verified_with)}"
            else:
                verification = ""
            upcoming.append(
                f"• {symbol}\n  {label}: {event.earnings_date:%b} {event.earnings_date.day}, {event.earnings_date:%Y} "
                f"({source}{verification})"
            )
    if not snapshots and not news_lines:
        return None
    sections = ["**MORNING INTELLIGENCE**"]
    if snapshots:
        sections.extend(["", "**WATCHLIST SNAPSHOT**", *snapshots])
        additional = max(0, len(symbols) - len(set(surfaced_symbols)))
        if additional:
            sections.append(f"{additional} additional companies monitored.")
    if news_lines:
        sections.extend(["", "**WHAT MATTERS**", *news_lines[:3]])
    if upcoming:
        sections.extend(["", "**UPCOMING**", *upcoming[:3]])
    if observations:
        unique = []
        for _, line in sorted(observations, reverse=True):
            if line not in unique:
                unique.append(line)
        sections.extend(["", "**ATLAS WATCH**", *unique[:2]])
    if market_dates:
        sections.extend(["", f"Market data — latest verified session: {max(market_dates)}"])
    if news_times:
        sections.append(f"News updated through: {max(news_times)}")
    if sources:
        sections.append(f"Sources: {', '.join(sorted(sources))}")
    return "\n".join(sections)
