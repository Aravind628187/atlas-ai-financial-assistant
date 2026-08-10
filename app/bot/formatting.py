"""Safe Telegram HTML rendering with a plain-text fallback."""
from __future__ import annotations

import html
import re


def telegram_html(text: str) -> str:
    """Escape all input first, then enable Atlas' small trusted markup subset."""
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped, flags=re.DOTALL)
    escaped = re.sub(r"(?<!\*)_(.+?)_(?!\*)", r"<i>\1</i>", escaped, flags=re.DOTALL)
    return escaped


def telegram_plain_text(text: str) -> str:
    return text.replace("**", "").replace("_", "")


async def reply_with_html_fallback(reply_text, text: str) -> None:
    try:
        await reply_text(telegram_html(text), parse_mode="HTML")
    except Exception:
        await reply_text(telegram_plain_text(text))


async def send_with_html_fallback(send_message, *, chat_id: int, text: str) -> None:
    try:
        await send_message(chat_id=chat_id, text=telegram_html(text), parse_mode="HTML")
    except Exception:
        await send_message(chat_id=chat_id, text=telegram_plain_text(text))
