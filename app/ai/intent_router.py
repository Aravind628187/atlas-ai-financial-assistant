"""
Routes a free-text user message to a structured intent + entities.

This is what lets the bot feel "agentic" rather than a single giant prompt:
Gemini first decides WHAT the user wants and pulls out tickers/companies,
then the appropriate service (market data, news, documents, ...) is called
to fetch real facts, and only THEN do we ask Gemini to write the reply —
grounded in real data instead of hallucinated numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.ai.gemini_client import gemini
from app.ai.prompts import INTENT_ROUTER_SYSTEM


@dataclass
class RoutedIntent:
    intent: str
    symbols: list[str] = field(default_factory=list)
    companies: list[str] = field(default_factory=list)
    needs_clarification: bool = False
    clarifying_question: str = ""


def route(user_message: str) -> RoutedIntent:
    data = gemini.generate_json(user_message, system_instruction=INTENT_ROUTER_SYSTEM)
    if not data:
        return RoutedIntent(intent="chitchat")
    return RoutedIntent(
        intent=data.get("intent", "chitchat"),
        symbols=[s.upper() for s in data.get("symbols", []) if isinstance(s, str)],
        companies=[c for c in data.get("companies", []) if isinstance(c, str)],
        needs_clarification=bool(data.get("needs_clarification", False)),
        clarifying_question=data.get("clarifying_question", "") or "",
    )
