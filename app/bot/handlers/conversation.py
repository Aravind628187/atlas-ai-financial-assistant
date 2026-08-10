from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.ai.brain import handle_text_turn
from app.bot.handlers.common import chunk_for_telegram, get_or_create_user
from app.bot.formatting import reply_with_html_fallback
from app.database import get_session

logger = logging.getLogger("atlas.bot.conversation")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    def process_turn() -> str:
        with get_session() as db:
            user, _ = get_or_create_user(
                db,
                telegram_id=update.effective_user.id,
                username=update.effective_user.username,
                first_name=update.effective_user.first_name,
            )
            return handle_text_turn(db, user, message.text)

    try:
        reply = await asyncio.to_thread(process_turn)
    except Exception:
        logger.exception("Unhandled text-turn failure")
        reply = "I hit a temporary service error. Your message wasn't lost—please try again shortly."

    for chunk in chunk_for_telegram(reply):
        await reply_with_html_fallback(message.reply_text, chunk)
