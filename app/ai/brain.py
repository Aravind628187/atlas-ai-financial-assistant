"""Atlas request pipeline: verify, calculate deterministically, generate last."""
from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import asdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.ai import onboarding
from app.ai.gemini_client import gemini
from app.ai.financial_response_validator import validate_financial_response
from app.ai.intent_router import RoutedIntent, route
from app.ai.memory import extract_and_store_personalization, get_preferences, get_recent_history, log_message, upsert_preference
from app.ai.prompts import ASSISTANT_PERSONA
from app.models import Alert, BriefingLog, Document, OnboardingStage, Preference, ResponseValidationLog, User, WatchlistItem
from app.services import google_integrations, news_service
from app.services.briefing_service import build_morning_brief
from app.services.document_service import answer_question_about_document
from app.services.entity_resolution import resolve_entities
from app.services.financial_calculator import profit_loss
from app.services.financial_data_gateway import gateway
from app.services.news_relevance import is_verified_catalyst
from app.services.providers.base import (
    CompanyProfile, DataStatus, EarningsData, FilingsData, FundamentalsData, HistoricalData, NewsItem, QuoteData,
)
from app.services.top_companies_service import (
    TopCompany, TopCompaniesRanking, normalize_top_symbol, share_class_key, top_companies_service,
)


logger = logging.getLogger("atlas.brain")
THRESHOLD_RE = re.compile(r"(\d+(?:\.\d+)?)\s*%")
MONEY_RE = r"[$₹€£]?\s*([\d,]+(?:\.\d+)?)"


def _fmt_number(value: float | None, digits: int = 2) -> str:
    return "—" if value is None else f"{value:,.{digits}f}"


def _fmt_compact(value: float | None) -> str:
    if value is None:
        return "—"
    for threshold, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M")):
        if abs(value) >= threshold:
            return f"{value / threshold:,.2f}{suffix}"
    return f"{value:,.2f}"


def _provider_label(value: str | None) -> str:
    if not value:
        return "Unknown"
    return {"fmp": "FMP", "finnhub": "Finnhub", "alpha_vantage": "Alpha Vantage"}.get(value.lower(), value)


def _as_of(result) -> str:
    if getattr(result, "timestamp_kind", None) == "daily_bar_date" and getattr(result, "data_date", None):
        return f"session {result.data_date.isoformat()}"
    if not result.data_as_of:
        return "timestamp unavailable"
    value = result.data_as_of.astimezone(dt.timezone.utc)
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _freshness_label(result) -> str:
    freshness = (result.verification or {}).get("freshness", "latest_available")
    return freshness.replace("_", " ").title()


def _symbols_from_context(db: Session, user: User, text: str, routed: RoutedIntent) -> list[str]:
    if routed.symbols:
        return routed.symbols
    if not any(phrase in text.lower() for phrase in ("which one", "which has", "what about", "how about", "them", "it ", "that company")):
        return []
    history = get_recent_history(db, user, limit=8)
    for turn in reversed(history):
        if turn["role"] == "user":
            symbols = resolve_entities(turn["content"]).symbols
            if symbols:
                return symbols
    return []


def _quote_reply(symbol: str) -> str:
    result = gateway.get_quote(symbol)
    quote = result.data
    if result.status == DataStatus.UNAVAILABLE or not isinstance(quote, QuoteData) or quote.price is None:
        if (result.verification or {}).get("disagreement"):
            return f"Two sources currently disagree on **{symbol}**, so I won't present one price as definitive."
        return f"I can't verify a sufficiently reliable **{symbol}** quote right now, so I won't provide an exact price."
    if result.status == DataStatus.STALE:
        return (
            f"I found a **{symbol}** quote, but it is stale for the current session. "
            "I won't present it as a current price."
        )
    currency = quote.currency or ""
    session = (result.market_status or (result.verification or {}).get("market_session") or "closed").lower()
    title = "Currently Trading" if session == "open" and result.is_realtime else "Latest Available"
    lines = [f"**{symbol} — {title}**", "", f"Price: **{_fmt_number(quote.price)} {currency}**".rstrip()]
    if quote.change is not None and quote.change_pct is not None:
        lines.append(f"Move: **{quote.change:+,.2f} ({quote.change_pct:+.2f}%)**")
    if quote.previous_close is not None:
        lines.append(f"Previous close: {_fmt_number(quote.previous_close)} {currency}".rstrip())
    lines.append(f"Market status: {session.replace('_', ' ').title()}")
    if session == "closed":
        lines.append("The US market is currently closed.")
    if result.timestamp_kind == "daily_bar_date" and result.data_date:
        lines.append(f"Session: {result.data_date.isoformat()}")
    else:
        lines.append(f"As of: {_as_of(result)}")
    lines.append(f"Source: {result.source}")
    verified_with = (result.verification or {}).get("secondary_source")
    if verified_with and (result.verification or {}).get("verified_fields") and not (result.verification or {}).get("disagreement"):
        lines.append(f"Verified with: {verified_with}")
    return "\n".join(lines)


def _fundamentals_reply(symbol: str, request: str) -> str:
    result = gateway.get_fundamentals(symbol)
    data = result.data
    if result.status == DataStatus.UNAVAILABLE or not isinstance(data, FundamentalsData):
        return f"I can't verify the requested fundamentals for **{symbol}** right now."
    requested = request.lower()
    fields = [
        ("Market cap", data.market_cap, lambda v: _fmt_compact(v), ("market cap", "valuation")),
        ("Trailing P/E", data.trailing_pe, lambda v: _fmt_number(v), ("p/e", "pe ratio", "valuation")),
        ("Forward P/E", data.forward_pe, lambda v: _fmt_number(v), ("forward pe", "forward p/e", "valuation")),
        ("TTM revenue" if data.revenue_period == "ttm" else "Revenue", data.revenue, lambda v: _fmt_compact(v), ("revenue",)),
        ("TTM net profit margin" if data.margin_period == "ttm" else "Profit margin", data.profit_margin, lambda v: f"{v * 100:.2f}%", ("profit margin", "margin")),
        ("TTM YoY revenue growth" if data.revenue_growth_period == "ttm_yoy" else "Quarterly YoY revenue growth" if data.revenue_growth_period == "quarterly_yoy" else "Revenue growth", data.revenue_growth, lambda v: f"{v * 100:.2f}%", ("revenue growth", "growth")),
        ("TTM dividend yield", data.dividend_yield_ttm, lambda v: f"{v * 100:.2f}%", ("dividend", "yield")),
        ("Forward dividend yield", data.dividend_yield_forward, lambda v: f"{v * 100:.2f}%", ("dividend", "yield")),
        ("Indicated dividend yield", data.dividend_yield_indicated, lambda v: f"{v * 100:.2f}%", ("dividend", "yield")),
        ("52-week high", data.fifty_two_week_high, lambda v: _fmt_number(v), ("52-week", "52 week")),
        ("52-week low", data.fifty_two_week_low, lambda v: _fmt_number(v), ("52-week", "52 week")),
    ]
    selected = [item for item in fields if any(term in requested for term in item[3])]
    selected = selected or fields
    available = [(label, formatter(value)) for label, value, formatter, _ in selected if value is not None]
    if not available:
        return f"**{symbol}:** the provider did not return that field, so I won't fill it from model memory."
    body = "\n".join(f"• {label}: **{value}**" for label, value in available)
    return f"**{symbol} — Verified fundamentals**\n\n{body}\n\nSource: {result.source}\nRetrieved: {result.retrieved_at:%Y-%m-%d %H:%M UTC}"


def _news_reply(symbol: str) -> str:
    quote_result = gateway.get_quote(symbol)
    news_result = gateway.get_news(symbol, 5)
    lines = [f"**{symbol} — Latest verified context**"]
    quote = quote_result.data
    if isinstance(quote, QuoteData) and quote.change_pct is not None and quote_result.status not in {DataStatus.UNAVAILABLE, DataStatus.STALE}:
        lines.extend(["", f"Latest move: **{quote.change_pct:+.2f}%** as of {_as_of(quote_result)} ({quote_result.source})"])
    items = news_result.data if isinstance(news_result.data, list) else []
    if not items:
        lines.extend(["", "I found no sufficiently relevant reporting, so I couldn't verify a specific recent catalyst."])
        return "\n".join(lines)
    catalysts = [item for item in items if isinstance(item, NewsItem) and is_verified_catalyst(item, symbol)]
    lines.extend(["", "**Reported catalyst**" if catalysts else "**Relevant developments**"])
    for item in items[:3]:
        if not isinstance(item, NewsItem):
            continue
        published = item.published_at.strftime("%Y-%m-%d %H:%M UTC") if item.published_at else "time unavailable"
        lines.append(f"• {item.headline}\n  {item.publisher or news_result.source} · {published}")
    if not catalysts:
        move = f"{quote.change_pct:+.2f}%" if isinstance(quote, QuoteData) and quote.change_pct is not None else "available but not causally explained"
        lines.append(f"\n{symbol}'s latest verified move is {move}. I found relevant {symbol}-related developments, but I couldn't verify a specific catalyst that explains the move.")
    return "\n".join(lines)


def _market_news_reply() -> str:
    routed = gateway.get_market_news(6)
    routed_items = routed.data if isinstance(routed.data, list) else []
    if routed_items:
        lines = ["**Retrieved market news**", ""]
        for item in routed_items[:6]:
            if isinstance(item, NewsItem):
                published = item.published_at.strftime("%Y-%m-%d %H:%M UTC") if item.published_at else "time unavailable"
                lines.append(f"• {item.headline}\n  {item.publisher or routed.source} · {published}")
        lines.extend(["", f"Source: {routed.source}", "No catalyst beyond these retrieved headlines has been inferred."])
        return "\n".join(lines)
    items = news_service.search_market_news(limit=6)
    if not items:
        return "I couldn't retrieve sufficiently recent market news, so I won't invent a market narrative."
    source = "NewsAPI" if settings.news_api_key else "Google News RSS"
    lines = ["**Retrieved market news**", ""]
    for item in items:
        lines.append(f"• {item.get('title')}\n  {item.get('publisher') or source} · {item.get('published') or 'time unavailable'}")
    lines.extend(["", "No catalyst beyond these retrieved headlines has been inferred."])
    return "\n".join(lines)


def _earnings_reply(symbol: str) -> str:
    result = gateway.get_earnings(symbol)
    data = result.data
    if not isinstance(data, EarningsData) or not data.earnings_date or data.status == "unverified":
        return f"**{symbol}**\n\nEarnings date: not yet verified."
    label = "Estimated earnings" if data.status == "estimated" else "Earnings"
    lines = [f"**{symbol} — Next earnings**", "", f"{label}: **{data.earnings_date:%b} {data.earnings_date.day}, {data.earnings_date:%Y}**",
             f"Status: {data.status.title()}", f"Source: {_provider_label(data.source or result.source)}"]
    if data.verified_with:
        corroboration_label = "Estimate corroborated by" if data.status == "estimated" else "Verified with"
        lines.append(f"{corroboration_label}: {_provider_label(data.verified_with)}")
    return "\n".join(lines)


def _historical_reply(symbol: str, text: str) -> str:
    value = text.lower()
    period = "1y" if "year" in value else "6mo" if "6 month" in value or "six month" in value else "3mo" if "3 month" in value else "1mo"
    result = gateway.get_history(symbol, period)
    data = result.data
    if not isinstance(data, HistoricalData) or len(data.points) < 2:
        return f"I can't verify enough historical **{symbol}** observations for that period."
    closes = [point.close for point in data.points if point.close is not None]
    highs = [point.high for point in data.points if point.high is not None]
    lows = [point.low for point in data.points if point.low is not None]
    if len(closes) < 2:
        return f"I can't calculate a reliable historical return for **{symbol}**."
    return_pct = (closes[-1] - closes[0]) / closes[0] * 100
    lines = [f"**{symbol} — {period} verified history**", "", f"Return: **{return_pct:+.2f}%**", f"Start close: {_fmt_number(closes[0])}", f"End close: {_fmt_number(closes[-1])}"]
    if highs:
        lines.append(f"Highest observed high: {_fmt_number(max(highs))}")
    if lows:
        lines.append(f"Lowest observed low: {_fmt_number(min(lows))}")
    peak = closes[0]
    max_drawdown = 0.0
    for close in closes:
        peak = max(peak, close)
        if peak:
            max_drawdown = min(max_drawdown, (close - peak) / peak * 100)
    lines.append(f"Maximum close-to-close drawdown: **{max_drawdown:.2f}%**")
    lines.extend([f"Through: {_as_of(result)}", f"Source: {result.source}"])
    return "\n".join(lines)


def _comparison_reply(symbols: list[str], text: str) -> str:
    if len(symbols) < 2:
        return "Which two companies or tickers should I compare?"
    rows: list[tuple[str, FundamentalsData, str]] = []
    for symbol in symbols[:4]:
        result = gateway.get_fundamentals(symbol)
        if isinstance(result.data, FundamentalsData):
            rows.append((symbol, result.data, result.source))
    if len(rows) < 2:
        return "I couldn't verify comparable fundamentals for both companies, so I won't guess which one has the higher valuation."
    requested = text.lower()
    pe_rows = [(symbol, data.trailing_pe) for symbol, data, _ in rows if data.trailing_pe is not None]
    if "higher valuation" in requested and len(pe_rows) >= 2:
        highest = max(pe_rows, key=lambda item: item[1])
        other = next(item for item in pe_rows if item[0] != highest[0])
        return f"On trailing P/E, **{highest[0]}** is higher at {_fmt_number(highest[1])} versus **{other[0]}** at {_fmt_number(other[1])}."
    lines = [f"**{' vs '.join(symbol for symbol, _, _ in rows)}**"]
    sections = [
        ("VALUATION", "Trailing P/E", "trailing_pe", lambda v: _fmt_number(v), None),
        ("MARKET CAP", "", "market_cap", lambda v: f"${_fmt_compact(v)}", None),
        ("TTM NET MARGIN", "TTM net profit margin", "profit_margin", lambda v: f"{v * 100:.2f}%", "ttm"),
        ("REVENUE GROWTH", "", "revenue_growth", lambda v: f"{v * 100:.2f}%", None),
        ("TTM REVENUE", "", "revenue", lambda v: f"${_fmt_compact(v)}", "ttm"),
    ]
    for heading, label, field, formatter, period in sections:
        values = []
        definitions = []
        for symbol, data, _ in rows:
            value = getattr(data, field)
            actual_period = (data.revenue_period or "ttm") if field == "revenue" else (data.margin_period or "ttm") if field == "profit_margin" else None
            definition = data.metric_definitions.get(field) or ("trailing" if field == "trailing_pe" else actual_period)
            if value is not None and (period is None or actual_period == period):
                values.append((symbol, value))
                definitions.append(definition)
        if len(values) != len(rows) or len(set(definitions)) > 1:
            continue
        lines.extend(["", f"**{heading}**"])
        lines.extend(f"{symbol}{f' {label.lower()}' if label else ''}: {formatter(value)}" for symbol, value in values)
    dividend_defs = [(data.metric_definitions.get("dividend_yield"), data) for _, data, _ in rows]
    if all(definition and definition != "unknown" for definition, _ in dividend_defs) and len({d for d, _ in dividend_defs}) == 1:
        definition = dividend_defs[0][0]
        field = {"ttm": "dividend_yield_ttm", "forward": "dividend_yield_forward", "indicated": "dividend_yield_indicated"}.get(definition)
        if field and all(getattr(data, field) is not None for _, data in dividend_defs):
            lines.extend(["", f"**{definition.upper()} DIVIDEND YIELD**"])
            for symbol, data, _ in rows:
                lines.append(f"{symbol}: {getattr(data, field) * 100:.2f}%")
    lines.extend(["", f"Sources: {', '.join(sorted({source for _, _, source in rows}))}"])
    return "\n".join(lines)


def _profile_reply(symbol: str, text: str) -> str:
    result = gateway.get_profile(symbol)
    profile = result.data
    if not isinstance(profile, CompanyProfile):
        return f"I can't verify a company profile for **{symbol}** right now."
    lines = [f"**{profile.name or symbol} ({symbol}) — Verified profile**", ""]
    if profile.sector:
        lines.append(f"Sector: {profile.sector}")
    if profile.industry:
        lines.append(f"Industry: {profile.industry}")
    if profile.summary:
        lines.extend(["", profile.summary[:700].strip()])
    lines.extend(["", f"Source: {result.source}", f"Retrieved: {result.retrieved_at:%Y-%m-%d %H:%M UTC}"])
    if any(word in text.lower() for word in ("should i buy", "should i invest")):
        lines.extend(["", "I can provide evidence and scenarios, but not a guaranteed or personalized buy recommendation. Consider concentration, horizon, liquidity, and downside tolerance."])
    return "\n".join(lines)


def _definition_reply(text: str) -> str | None:
    lowered = text.lower()
    if "p/e" in lowered or "pe ratio" in lowered:
        return (
            "**P/E ratio** compares a company's share price with its earnings per share. "
            "A higher P/E can reflect stronger growth expectations, but it can also mean a richer valuation. "
            "Compare it with peers, growth, margins, and the company's own history—not in isolation."
        )
    return None


def _top_company_count(text: str) -> int:
    match = re.search(r"\b(?:top|largest|biggest)\s+(\d{1,3})\b", text.lower())
    if not match:
        match = re.search(r"\b(\d{1,3})\s+(?:largest|biggest)\b", text.lower())
    return int(match.group(1)) if match else 15


def _top_source_label(source: str) -> str:
    return "FMP market-cap ranking" if source == "fmp" else "Fallback seed (not live)"


def _existing_share_classes(symbols: set[str]) -> dict[str, str]:
    return {share_class_key(symbol): symbol for symbol in symbols}


def _validated_top_companies(ranking: TopCompaniesRanking, limit: int,
                             existing_symbols: set[str]) -> tuple[list[TopCompany], list[str]]:
    """Validate in rank order, trying only necessary provider-specific variants."""
    selected: list[TopCompany] = []
    skipped: list[str] = []
    seen: set[str] = set()
    existing_classes = _existing_share_classes(existing_symbols)
    for company in ranking.companies:
        key = share_class_key(company.symbol, company.name)
        if key in seen:
            continue
        seen.add(key)
        if key in existing_classes:
            selected.append(TopCompany(existing_classes[key], company.name, company.market_cap))
            if len(selected) >= limit:
                break
            continue
        preferred = "GOOGL" if key == "ALPHABET" else normalize_top_symbol(company.symbol)
        variants = [preferred]
        if key == "BERKSHIRE":
            variants = ["BRK-B", "BRK.B"]
        valid_symbol = None
        for candidate in variants:
            try:
                result = gateway.get_quote(candidate, verify=False)
            except Exception:
                continue
            if isinstance(result.data, QuoteData) and result.data.price is not None and result.status not in {
                DataStatus.UNAVAILABLE, DataStatus.ERROR, DataStatus.STALE, DataStatus.CONFLICTING_DATA,
            }:
                valid_symbol = candidate
                break
        if valid_symbol:
            selected.append(TopCompany(valid_symbol, company.name, company.market_cap))
        else:
            skipped.append(preferred)
        if len(selected) >= limit:
            break
    return selected, skipped


def _save_top_context(db: Session, user: User, companies: list[TopCompany], ranking: TopCompaniesRanking) -> None:
    payload = json.dumps({
        "symbols": [company.symbol for company in companies], "source": ranking.source,
        "retrieved_at": ranking.retrieved_at.isoformat(),
    }, separators=(",", ":"))
    upsert_preference(db, user, "last_top_companies", payload[:512])


def _load_top_context(db: Session, user: User) -> tuple[list[TopCompany], str, dt.datetime] | None:
    raw = get_preferences(db, user).get("last_top_companies")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        symbols = [normalize_top_symbol(value) for value in payload.get("symbols", []) if value]
        retrieved = dt.datetime.fromisoformat(payload["retrieved_at"])
        source = str(payload.get("source") or "fallback_seed")
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return None
    return top_companies_service.companies_for_symbols(symbols), source, retrieved


def _get_validated_ranking(db: Session, user: User, count: int) -> tuple[list[TopCompany], list[str], TopCompaniesRanking]:
    existing = set(db.scalars(select(WatchlistItem.symbol).where(WatchlistItem.user_id == user.id)).all())
    fetch_count = min(25, count + 5)
    ranking = top_companies_service.get_top(fetch_count)
    companies, skipped = _validated_top_companies(ranking, count, existing)
    return companies, skipped, ranking


def _top_companies(db: Session, user: User, intent: str, text: str) -> str:
    count = _top_company_count(text)
    if count > 25:
        return "Please choose 25 companies or fewer because very large watchlists reduce briefing quality and consume unnecessary provider quota."
    count = max(1, count)
    follow_up = bool(re.fullmatch(r"(?:please\s+)?(?:track|add)\s+these[.!?]*", text.strip(), re.IGNORECASE))
    skipped: list[str] = []
    if follow_up:
        context = _load_top_context(db, user)
        if not context:
            return "Show me a top-companies ranking first, then say “track these.”"
        companies, source, retrieved_at = context
        ranking = TopCompaniesRanking(tuple(companies), source, retrieved_at, source == "fmp")
        count = len(companies)
    else:
        try:
            companies, skipped, ranking = _get_validated_ranking(db, user, count)
        except Exception as exc:
            logger.warning("Top-companies ranking failed safely: %s", type(exc).__name__)
            return "I couldn't retrieve and validate a top-companies ranking right now. Your watchlist was not changed."

    if intent == "top_companies_show":
        if not companies:
            return "I couldn't validate any companies from the available ranking, so I won't present an unverified list."
        _save_top_context(db, user, companies, ranking)
        title_count = count if len(companies) == count else len(companies)
        lines = [f"**Top {title_count} U.S. companies by market cap**", ""]
        lines.extend(f"{index}. **{company.symbol}** — {company.name}" for index, company in enumerate(companies, 1))
        lines.extend(["", f"Source: {_top_source_label(ranking.source)}",
                      f"Ranking retrieved: {ranking.retrieved_at:%Y-%m-%d %H:%M UTC}",
                      "", "Say “track these” if you want me to add them."])
        if skipped:
            lines.append(f"Skipped unsupported symbols: {', '.join(skipped)}")
        return "\n".join(lines)

    target_keys = {share_class_key(company.symbol, company.name) for company in companies}
    existing_rows = db.scalars(select(WatchlistItem).where(WatchlistItem.user_id == user.id)).all()
    if intent == "top_companies_remove":
        removed = [row for row in existing_rows if share_class_key(row.symbol) in target_keys]
        for row in removed:
            db.delete(row)
        db.flush()
        if not removed:
            return "None of the resolved top companies were on your watchlist. Unrelated watchlist items were left unchanged."
        return f"Removed **{len(removed)}** resolved top-companies item{'s' if len(removed) != 1 else ''}. Unrelated watchlist items were left unchanged."

    existing_by_class = _existing_share_classes({row.symbol for row in existing_rows})
    already = [existing_by_class[share_class_key(company.symbol, company.name)] for company in companies
               if share_class_key(company.symbol, company.name) in existing_by_class]
    missing = [company for company in companies if share_class_key(company.symbol, company.name) not in existing_by_class]
    capacity = max(0, settings.max_watchlist_items - len(existing_rows))
    to_add, capacity_skipped = missing[:capacity], missing[capacity:]
    for company in to_add:
        db.add(WatchlistItem(user_id=user.id, symbol=company.symbol, label=company.name))
    db.flush()

    lines = [f"**Top {count} U.S. companies added**", ""]
    lines.extend(f"✓ **{company.symbol}** — {company.name}" for company in to_add)
    if already:
        lines.extend(["", "Already tracking:", ", ".join(dict.fromkeys(already))])
    if capacity_skipped:
        lines.extend(["", f"Adding all {count} would take your watchlist above the {settings.max_watchlist_items}-company limit. "
                      "I added the largest available companies until the limit was reached."])
    all_skipped = skipped + [company.symbol for company in capacity_skipped]
    lines.extend(["", f"Added: {len(to_add)}", f"Already tracked: {len(set(already))}",
                  f"Skipped: {len(all_skipped)}", f"Source: {_top_source_label(ranking.source)}"])
    if all_skipped:
        lines.append(f"Skipped symbols: {', '.join(all_skipped)}")
    if "alert" in text.lower():
        alert_symbols = [company.symbol for company in companies]
        lines.extend(["", _alerts(db, user, "alert_create", alert_symbols, text)])
    return "\n".join(lines)


def _watchlist(db: Session, user: User, intent: str, symbols: list[str], text: str) -> str:
    if intent == "watchlist_show":
        rows = db.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id).order_by(WatchlistItem.added_at)).scalars().all()
        if not rows:
            return "Your watchlist is empty."
        return "**Your watchlist**\n\n" + "\n".join(
            f"• **{row.symbol}**" + (f" — {row.label}" if row.label else "") for row in rows
        )
    clear_all = any(phrase in text.lower() for phrase in ("clear my watchlist", "remove everything from my watchlist", "empty my watchlist"))
    if intent == "watchlist_remove" and clear_all:
        rows = db.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id)).scalars().all()
        for row in rows:
            db.delete(row)
        db.flush()
        return f"Cleared **{len(rows)}** watchlist item{'s' if len(rows) != 1 else ''}." if rows else "Your watchlist is already empty."
    if not symbols:
        return "Which company or ticker should I update on your watchlist?"
    if intent == "watchlist_remove":
        rows = db.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id, WatchlistItem.symbol.in_(symbols))).scalars().all()
        for row in rows:
            db.delete(row)
        db.flush()
        removed = [row.symbol for row in rows]
        return f"Removed **{', '.join(removed)}** from your watchlist." if removed else "None of those symbols were on your watchlist."
    label_match = re.search(r"\b(?:as|label(?:led)?|called)\s+[\"']?([^\"'.]+)", text, re.IGNORECASE)
    label = label_match.group(1).strip()[:128] if label_match else None
    added, existing, invalid, limited = [], [], [], []
    current_count = len(db.scalars(select(WatchlistItem).where(WatchlistItem.user_id == user.id)).all())
    for symbol in symbols:
        present = db.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id, WatchlistItem.symbol == symbol)).scalar_one_or_none()
        if present:
            existing.append(symbol)
            continue
        if current_count + len(added) >= settings.max_watchlist_items:
            limited.append(symbol)
            continue
        check = gateway.get_quote(symbol, verify=False)
        if check.status == DataStatus.UNAVAILABLE or not isinstance(check.data, QuoteData):
            invalid.append(symbol)
            continue
        db.add(WatchlistItem(user_id=user.id, symbol=symbol, label=label))
        added.append(symbol)
    db.flush()
    parts = []
    if added:
        parts.append(f"Now tracking **{', '.join(added)}**.")
    if existing:
        parts.append(f"Already tracked: {', '.join(existing)}.")
    if invalid:
        parts.append(f"I couldn't validate {', '.join(invalid)}, so I didn't add it.")
    if limited:
        parts.append(f"I didn't add {', '.join(limited)} because your watchlist is limited to {settings.max_watchlist_items} companies.")
    return " ".join(parts)


def _alerts(db: Session, user: User, intent: str, symbols: list[str], text: str) -> str:
    all_alerts = db.execute(select(Alert).where(Alert.user_id == user.id)).scalars().all()
    active = [row for row in all_alerts if row.active]
    if intent == "alert_list":
        if not active:
            return "You have no active alerts."
        return "**Active percentage-movement alerts**\n\n" + "\n".join(f"• {row.symbol}: ±{row.threshold_pct or 5:g}%" for row in active)
    if intent == "alert_remove":
        targets = active if "all alerts" in text.lower() else [row for row in active if row.symbol in symbols]
        for row in targets:
            row.active = False
        db.flush()
        return f"Turned off **{len(targets)}** alert{'s' if len(targets) != 1 else ''}." if targets else "I couldn't find a matching active alert."
    if intent == "alert_update" and any(x in text.lower() for x in ("turn alerts back on", "enable all alerts", "resume all alerts", "reactivate alerts")):
        inactive = db.execute(select(Alert).where(Alert.user_id == user.id, Alert.active.is_(False))).scalars().all()
        for row in inactive:
            row.active = True
        db.flush()
        return f"Turned **{len(inactive)}** alert{'s' if len(inactive) != 1 else ''} back on." if inactive else "You have no disabled alerts to turn back on."
    if not symbols:
        return "Which symbol should the percentage-movement alert watch?"
    match = THRESHOLD_RE.search(text)
    threshold = float(match.group(1)) if match else 5.0
    if not 0 < threshold <= 100:
        return "Use a percentage threshold greater than 0 and no more than 100%."
    changed, created = [], []
    for symbol in symbols:
        row = next((item for item in all_alerts if item.symbol == symbol and item.kind == "pct_move"), None)
        if row:
            if not row.active:
                row.active = True
                row.threshold_pct = threshold
                created.append(symbol)
            elif intent == "alert_update" or row.threshold_pct != threshold:
                row.threshold_pct = threshold
                changed.append(symbol)
            continue
        if intent == "alert_update":
            continue
        check = gateway.get_quote(symbol, verify=False)
        if check.status == DataStatus.UNAVAILABLE:
            continue
        db.add(Alert(user_id=user.id, symbol=symbol, kind="pct_move", threshold_pct=threshold, active=True))
        created.append(symbol)
    db.flush()
    if changed:
        return f"Updated **{', '.join(changed)}** to a ±{threshold:g}% session-move alert."
    if created:
        return f"Alert armed for **{', '.join(created)}** at ±{threshold:g}% session movement."
    return "No alert was changed. The symbol may be invalid, unavailable, or already has that active threshold."


def _filings_reply(symbol: str, text: str) -> str:
    lowered = text.lower()
    forms = ("10-Q",) if "10-q" in lowered else ("10-K",) if "10-k" in lowered else ("8-K",) if "8-k" in lowered else ("10-Q", "10-K", "8-K")
    wants_history = any(word in lowered for word in ("recent filings", "filing history", "last filings", "all filings"))
    result = gateway.get_filings(symbol, forms, 5 if wants_history else 1)
    data = result.data
    if result.status == DataStatus.UNAVAILABLE or not isinstance(data, FilingsData) or not data.filings:
        return f"I couldn't verify a recent SEC filing for **{symbol}** right now."
    if not wants_history and len(data.filings) == 1:
        filing = data.filings[0]
        lines = [f"**{data.company_name} — Latest {filing.form}**", "", "Filed:", filing.filed_at.isoformat()]
        if filing.report_date:
            lines.extend(["", "Reporting period:", filing.report_date.isoformat()])
        lines.extend(["", "Source:", "SEC EDGAR"])
        if filing.url:
            lines.extend(["", "Filing:", filing.url])
        lines.extend(["", "Ask me to summarize it or compare it with the previous quarter."])
        return "\n".join(lines)
    lines = [f"**{data.company_name} ({symbol}) — SEC filings**", ""]
    for filing in data.filings:
        report = f" · period {filing.report_date.isoformat()}" if filing.report_date else ""
        link = f"\n  {filing.url}" if filing.url else ""
        lines.append(f"• **{filing.form}** · filed {filing.filed_at.isoformat()}{report}{link}")
    lines.extend(["", f"Source: {result.source}", f"Retrieved: {result.retrieved_at:%Y-%m-%d %H:%M UTC}"])
    return "\n".join(lines)


def _calculation_reply(text: str) -> str:
    normalized = text.lower().replace(",", "")
    trade = re.search(r"(?:bought|buy)\s+([\d.]+)\s+shares?.*?(?:at|for)\s*[$₹€£]?\s*([\d.]+).*?(?:sold|sell).*?(?:at|for)\s*[$₹€£]?\s*([\d.]+)", normalized)
    if trade:
        shares, buy, sell = map(float, trade.groups())
        result = profit_loss(shares, buy, sell)
        return (
            "**Deterministic calculation**\n\n"
            f"Cost: ${result['cost']:,.2f}\nProceeds: ${result['proceeds']:,.2f}\n"
            f"Profit: **${result['profit']:,.2f}**\nReturn: **{result['return_pct']:.2f}%**"
        )
    gain = re.search(r"([\d.]+)\s*%\s*(?:gain|return)\s*(?:on|of)\s*[$₹€£]?\s*([\d.]+)", normalized)
    if gain:
        pct, principal = map(float, gain.groups())
        amount = principal * pct / 100
        return f"**Deterministic calculation**\n\nGain: **${amount:,.2f}**\nEnding value: **${principal + amount:,.2f}**"
    return "I can calculate that deterministically, but I need the exact inputs (amounts, rates, dates, or share prices)."


def _briefing(db: Session, user: User) -> str:
    watchlist = db.execute(select(WatchlistItem).where(WatchlistItem.user_id == user.id)).scalars().all()
    preferences = db.execute(select(Preference).where(Preference.user_id == user.id)).scalars().all()
    brief = build_morning_brief(user, watchlist, preferences)
    if not brief:
        return "I don't have enough verified market data to build a useful briefing right now."
    db.add(BriefingLog(user_id=user.id, kind="morning_brief", content=brief))
    db.flush()
    return brief


def _document_reply(db: Session, user: User, text: str) -> str:
    document = db.get(Document, user.last_document_id) if user.last_document_id else None
    if not document or not document.extracted_text:
        return "Upload or select a text-extractable document first. I won't mix facts across documents."
    try:
        return answer_question_about_document(document.extracted_text, text)
    except Exception:
        logger.exception("Document question failed user=%s document=%s", user.id, document.id)
        return "I couldn't analyze that document right now. Please try again shortly."


def _general_reply(text: str, history: list[dict], preferences: dict) -> str:
    if any(word in text.lower() for word in ("today", "current", "latest", "right now")):
        return "I don't have a verified live source for that non-financial question, so I won't present a current claim as fact."
    try:
        return gemini.generate(
            f"User preferences: {preferences}\nUser message: {text}",
            system_instruction=ASSISTANT_PERSONA, history=history[-6:], temperature=0.4,
        )
    except Exception:
        return "I can't reach the language service right now. Financial commands and deterministic calculations are still available."


def _handle_completed_user(db: Session, user: User, text: str, routed: RoutedIntent) -> str:
    symbols = _symbols_from_context(db, user, text, routed)
    intent = routed.intent
    lowered = text.lower()
    if "keep answers short" in lowered or "prefer short" in lowered:
        upsert_preference(db, user, "response_mode", "brief")
        return "Got it — I'll keep answers brief."
    if "detailed analysis" in lowered or "detailed answers" in lowered:
        upsert_preference(db, user, "response_mode", "detailed")
        return "Got it — I'll use detailed mode."
    if "beginner mode" in lowered or "explain like i'm a beginner" in lowered or "explain like i am a beginner" in lowered:
        upsert_preference(db, user, "response_mode", "beginner")
        if intent not in {"definitions", "general_chat"}:
            return "Got it — I'll use beginner-friendly explanations and define financial terms."
    if any(phrase in lowered for phrase in ("all my money", "guaranteed return", "definitely make")):
        return (
            "Putting all your money into one stock creates severe concentration risk. I can provide verified company data and scenario analysis, "
            "but I can't guarantee returns or recommend an all-in allocation. Diversification, time horizon, liquidity needs, and loss tolerance all matter."
        )
    if intent in {"market_quote", "market_move"}:
        return _quote_reply(symbols[0]) if symbols else "Which company or ticker do you want a verified quote for?"
    if intent == "company_news":
        return _news_reply(symbols[0]) if symbols else "Which company should I retrieve current news for?"
    if intent == "market_news":
        return _market_news_reply()
    if intent == "company_fundamentals":
        if len(symbols) > 1 and any(x in lowered for x in ("what about", "how about", "which")):
            return _comparison_reply(symbols, text)
        return _fundamentals_reply(symbols[0], text) if symbols else "Which company or ticker do you want fundamentals for?"
    if intent == "earnings":
        return _earnings_reply(symbols[0]) if symbols else "Which company's earnings date should I verify?"
    if intent == "historical_price":
        return _historical_reply(symbols[0], text) if symbols else "Which ticker and period should I analyze?"
    if intent == "company_comparison":
        return _comparison_reply(symbols, text)
    if intent == "company_profile":
        return _profile_reply(symbols[0], text) if symbols else "Which company should I research?"
    if intent in {"top_companies_show", "top_companies_add", "top_companies_remove"}:
        return _top_companies(db, user, intent, text)
    if intent in {"watchlist_add", "watchlist_remove", "watchlist_show"}:
        return _watchlist(db, user, intent, symbols, text)
    if intent in {"alert_create", "alert_list", "alert_remove", "alert_update"}:
        return _alerts(db, user, intent, symbols, text)
    if intent == "financial_calculation" or intent == "portfolio_math":
        return _calculation_reply(text)
    if intent == "briefing":
        return _briefing(db, user)
    if intent == "schedule_briefing":
        hour = re.search(r"\b([01]?\d|2[0-3])(?::\d{2})?\s*(am|pm)?\b", lowered)
        if not hour:
            return "What local hour should I use for your daily briefing? Include AM or PM."
        value = int(hour.group(1))
        if hour.group(2) == "pm" and value < 12:
            value += 12
        if hour.group(2) == "am" and value == 12:
            value = 0
        user.briefing_hour_local = value
        return f"Your daily briefing is scheduled for **{value:02d}:00** in **{user.timezone}**."
    if intent == "document_question":
        return _document_reply(db, user, text)
    if intent == "filings":
        return _filings_reply(symbols[0], text) if symbols else "Which company's SEC filings should I retrieve?"
    if intent == "definitions":
        defined = _definition_reply(text)
        if defined:
            return defined
    if intent == "integration_connect":
        if not google_integrations.is_connection_available():
            configured = "Client credentials are present, but " if google_integrations.is_configured() else ""
            return f"{configured}Google token exchange is not implemented on this Atlas instance, so I can't claim the integration is connected."
        return "Google integration connection is available through the configured administrator consent flow."
    preferences = get_preferences(db, user)
    history = get_recent_history(db, user)
    generated = _general_reply(text, history, preferences)
    if intent in {"economic_question", "unsupported_or_uncertain"}:
        validation = validate_financial_response(generated, [])
        db.add(ResponseValidationLog(
            user_id=user.id, intent=intent,
            result="accepted" if validation.valid else "blocked",
            unsupported_claims={"claims": validation.unsupported_claims} if validation.unsupported_claims else None,
        ))
        db.flush()
        if not validation.valid:
            return "I couldn't verify the numerical claims needed for that answer, so I won't present them as fact."
    return generated


def handle_text_turn(db: Session, user: User, text: str, input_kind: str = "text") -> str:
    """Stable public entry point used by text and voice handlers."""
    user.last_active_at = dt.datetime.utcnow()
    log_message(db, user, "user", text, input_kind=input_kind)
    if user.onboarding_stage != OnboardingStage.DONE.value:
        try:
            reply = onboarding.handle_onboarding_turn(db, user, text)
        except Exception:
            logger.exception("Onboarding failed for user=%s", user.id)
            reply = "I couldn't process that setup answer right now. Please try again."
        log_message(db, user, "assistant", reply, intent="onboarding")
        return reply
    routed = route(text)
    if routed.needs_clarification and routed.clarifying_question:
        reply = routed.clarifying_question
    else:
        try:
            reply = _handle_completed_user(db, user, text, routed)
        except Exception:
            logger.exception("request_failed user=%s intent=%s", user.id, routed.intent)
            reply = "I couldn't verify the requested information right now, so I won't guess. Please try again shortly."
    log_message(db, user, "assistant", reply, intent=routed.intent)
    extract_and_store_personalization(db, user, text)
    return reply
