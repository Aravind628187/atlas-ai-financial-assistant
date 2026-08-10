from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.ai.brain import handle_text_turn
from app.ai.gemini_client import GeminiUnavailableError, gemini
from app.bot.handlers.common import chunk_for_telegram, get_or_create_user
from app.bot.formatting import reply_with_html_fallback
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
        transcript = await asyncio.to_thread(gemini.transcribe_and_understand, audio_bytes, "audio/ogg")
    except GeminiUnavailableError:
        logger.warning("Voice transcription is temporarily unavailable")
        await message.reply_text("Voice transcription is temporarily unavailable. Please type your request—quotes, alerts, watchlists, and calculations still work.")
        return
    except Exception:
        logger.exception("Voice transcription failed")
        await message.reply_text("I couldn't quite make that out — mind typing it instead?")
        return

    if not transcript:
        await message.reply_text("I couldn't quite make that out — mind typing it instead?")
        return

    def process_turn() -> str:
        with get_session() as db:
            user, _ = get_or_create_user(
                db,
                telegram_id=update.effective_user.id,
                username=update.effective_user.username,
                first_name=update.effective_user.first_name,
            )
            return handle_text_turn(db, user, transcript, input_kind="voice")

    try:
        reply = await asyncio.to_thread(process_turn)
    except Exception:
        logger.exception("Unhandled voice-turn failure")
        reply = "I hit a temporary service error after transcribing that. Please try again shortly."

    for chunk in chunk_for_telegram(reply):
        await reply_with_html_fallback(message.reply_text, chunk)
