from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.ai.brain import handle_text_turn
from app.bot.handlers.common import chunk_for_telegram, get_or_create_user
from app.database import get_session

logger = logging.getLogger("atlas.bot.conversation")


async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message or not message.text:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    with get_session() as db:
        user, _ = get_or_create_user(
            db,
            telegram_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )
        reply = handle_text_turn(db, user, message.text)

    for chunk in chunk_for_telegram(reply):
        try:
            await message.reply_text(chunk, parse_mode="Markdown")
        except Exception:  # noqa: BLE001 — malformed markdown from the model shouldn't break the reply
            await message.reply_text(chunk)
