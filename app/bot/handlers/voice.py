from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.ai.brain import handle_text_turn
from app.ai.gemini_client import gemini
from app.bot.handlers.common import chunk_for_telegram, get_or_create_user
from app.database import get_session

logger = logging.getLogger("atlas.bot.voice")


async def handle_voice_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    voice = message.voice or message.audio
    if not voice:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    tg_file = await context.bot.get_file(voice.file_id)
    audio_bytes = bytes(await tg_file.download_as_bytearray())

    try:
        transcript = gemini.transcribe_and_understand(audio_bytes, mime_type="audio/ogg")
    except Exception:
        logger.exception("Voice transcription failed")
        await message.reply_text("I couldn't quite make that out — mind typing it instead?")
        return

    if not transcript:
        await message.reply_text("I couldn't quite make that out — mind typing it instead?")
        return

    with get_session() as db:
        user, _ = get_or_create_user(
            db,
            telegram_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )
        reply = handle_text_turn(db, user, transcript, input_kind="voice")

    for chunk in chunk_for_telegram(reply):
        try:
            await message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await message.reply_text(chunk)
