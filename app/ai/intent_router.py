"""Rate-conscious deterministic routing with a validated Gemini fallback."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.ai.gemini_client import gemini
from app.ai.prompts import INTENT_ROUTER_SYSTEM
from app.services.entity_resolution import resolve_entities


VALID_INTENTS = {
    "general_chat", "market_quote", "market_move", "company_profile", "company_comparison",
    "company_fundamentals", "historical_price", "market_news", "company_news", "earnings",
    "watchlist_add", "watchlist_remove", "watchlist_show", "alert_create", "alert_list",
    "alert_remove", "alert_update", "briefing", "schedule_briefing", "document_summary",
    "document_question", "portfolio_math", "financial_calculation", "definitions",
    "economic_question", "integration_connect", "filings", "unsupported_or_uncertain", "clarify",
    "top_companies_show", "top_companies_add", "top_companies_remove",
}


@dataclass(slots=True)
class RoutedIntent:
    intent: str
    symbols: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarifying_question: str = ""
    ai_called: bool = False


def _deterministic_intent(text: str) -> str | None:
    value = " ".join(text.lower().split())
    top_company_request = bool(
        re.search(r"\b(?:top|largest|biggest)\b", value)
        and re.search(r"\b(?:compan(?:y|ies)|stocks?|equities|market cap)\b", value)
    )
    if re.fullmatch(r"(?:please\s+)?(?:track|add)\s+these[.!?]*", value):
        return "top_companies_add"
    if top_company_request:
        if re.search(r"\b(?:remove|delete|stop tracking)\b", value):
            return "top_companies_remove"
        if re.search(r"\b(?:track|add|follow|monitor)\b", value):
            return "top_companies_add"
        return "top_companies_show"
    if re.fullmatch(r"(?:tell me about|research|look into)\s+(?:apple|nvidia|microsoft|amazon|alphabet|google|tesla|meta|amd|netflix|broadcom|intel|oracle)[?.!]*", value):
        return "clarify"
    if any(x in value for x in ("latest filing", "latest 10-q", "latest 10-k", "latest 8-k", "sec filing")):
        return "filings"
    if any(x in value for x in ("clear my watchlist", "remove everything from my watchlist", "empty my watchlist")):
        return "watchlist_remove"
    if any(x in value for x in ("show my watchlist", "what am i tracking", "companies am i tracking", "my watchlist")):
        return "watchlist_show"
    if any(x in value for x in ("stop tracking", "remove from my watchlist", "remove ")) and "alert" not in value:
        return "watchlist_remove"
    if any(x in value for x in ("track ", "add ", "keep an eye on")) and "alert" not in value:
        return "watchlist_add"
    if any(x in value for x in ("show my alerts", "active alerts", "what alerts", "list my alerts")):
        return "alert_list"
    if any(x in value for x in ("turn off all alerts", "remove all alerts", "disable all alerts")):
        return "alert_remove"
    if any(x in value for x in ("turn alerts back on", "enable all alerts", "resume all alerts", "reactivate alerts")):
        return "alert_update"
    if "alert" in value and any(x in value for x in ("change", "update", "set my")):
        return "alert_update"
    if re.search(r"\b(?:change|update|set)\b.*\d+(?:\.\d+)?\s*%", value) and resolve_entities(text).symbols:
        return "alert_update"
    if "alert" in value and any(x in value for x in ("remove", "turn off", "disable", "delete")):
        return "alert_remove"
    if any(x in value for x in ("alert me", "notify me")):
        return "alert_create"
    if "briefing" in value or "morning brief" in value:
        return "schedule_briefing" if any(x in value for x in ("at ", "change", "schedule", "every")) else "briefing"
    if any(x in value for x in ("last month", "last year", "past month", "past year", "historical", "highest", "lowest", "drawdown", "6 month", "six month")):
        return "historical_price"
    if any(x in value for x in ("profit", "loss", "cagr", "compound interest", "simple interest", "average cost", "drawdown", "position size", "% gain", "percent gain")):
        return "financial_calculation"
    if "earnings" in value and any(x in value for x in ("when", "date", "report", "next")):
        return "earnings"
    if any(x in value for x in ("news", "headline", "catalyst", "what's moving", "whats moving", "why is", "why did")):
        return "company_news" if resolve_entities(text).symbols else "market_news"
    if "compare" in value or "higher valuation" in value or "which one" in value:
        return "company_comparison"
    if any(x in value for x in ("what is p/e", "explain p/e", "what does p/e", "define ")):
        return "definitions"
    if any(x in value for x in ("p/e", "pe ratio", "market cap", "revenue", "profit margin", "margin", "52-week", "52 week", "forward pe", "valuation", "dividend yield", "revenue growth")):
        return "company_fundamentals"
    if any(x in value for x in ("price", "quote", "trading at", "performing today", "how is ")) and resolve_entities(text).symbols:
        return "market_quote"
    if any(x in value for x in ("all my money", "guaranteed return", "should i invest", "should i buy")):
        return "company_profile"
    if any(x in value for x in ("this document", "the document", "the report", "uploaded")):
        return "document_question"
    if value in {"hi", "hello", "hey", "thanks", "thank you", "who are you"}:
        return "general_chat"
    if value.startswith(("what is ", "explain ", "who is ", "how does ")):
        return "general_chat"
    finance_markers = (
        "stock", "market", "company", "ticker", "portfolio", "earnings", "revenue",
        "shares", "invest", "valuation", "watchlist", "alert", "briefing", "financial", "document",
    )
    if not any(marker in value for marker in finance_markers):
        return "general_chat"
    return None


def route(user_message: str) -> RoutedIntent:
    entities = resolve_entities(user_message)
    deterministic = _deterministic_intent(user_message)
    if deterministic:
        clarification = "Do you want Apple's price, recent news, fundamentals, filings, or a company overview?" if deterministic == "clarify" else ""
        return RoutedIntent(
            intent=deterministic, symbols=entities.symbols,
            needs_clarification=deterministic == "clarify", clarifying_question=clarification,
        )
    try:
        data = gemini.generate_json(user_message, system_instruction=INTENT_ROUTER_SYSTEM)
    except Exception:
        return RoutedIntent(intent="unsupported_or_uncertain", symbols=entities.symbols)
    if not isinstance(data, dict):
        return RoutedIntent(intent="unsupported_or_uncertain", symbols=entities.symbols, ai_called=True)
    intent = data.get("intent")
    if intent not in VALID_INTENTS:
        intent = "unsupported_or_uncertain"
    llm_symbols = data.get("symbols", []) if isinstance(data.get("symbols", []), list) else []
    resolved = resolve_entities(user_message, llm_symbols)
    companies = [str(c)[:128] for c in data.get("companies", []) if isinstance(c, str)] if isinstance(data, dict) else []
    return RoutedIntent(
        intent=intent, symbols=resolved.symbols, companies=companies,
        needs_clarification=bool(data.get("needs_clarification", False)),
        clarifying_question=str(data.get("clarifying_question", ""))[:300], ai_called=True,
    )
