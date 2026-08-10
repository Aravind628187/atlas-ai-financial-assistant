from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.ai.memory import log_message
from app.ai.gemini_client import GeminiUnavailableError
from app.ai.llm_gateway import SecondaryLLMUnavailableError
from app.bot.handlers.common import chunk_for_telegram, get_or_create_user
from app.bot.formatting import reply_with_html_fallback
from app.database import get_session
from app.models import Document, OnboardingStage
from app.services.document_service import extract_pdf_text, summarize_image, summarize_text

logger = logging.getLogger("atlas.bot.documents")

MAX_FILE_MB = 15


async def handle_document_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    doc = message.document
    if not doc:
        return

    if doc.file_size and doc.file_size > MAX_FILE_MB * 1024 * 1024:
        await message.reply_text(f"That file's a bit large for me right now (max {MAX_FILE_MB}MB) — try a smaller export?")
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    tg_file = await context.bot.get_file(doc.file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())
    filename = doc.file_name or "uploaded-document"

    is_pdf = (doc.mime_type == "application/pdf") or filename.lower().endswith(".pdf")

    def user_is_ready() -> bool:
        with get_session() as db:
            user, _ = get_or_create_user(
                db, telegram_id=update.effective_user.id,
                username=update.effective_user.username, first_name=update.effective_user.first_name,
            )
            return user.onboarding_stage == OnboardingStage.DONE.value

    if not await asyncio.to_thread(user_is_ready):
        await message.reply_text("Let's finish setting you up first — I'll be able to dig into documents right after!")
        return

    def process_file() -> tuple[str | None, str]:
        if is_pdf:
            extracted = extract_pdf_text(file_bytes)
            return extracted, summarize_text(extracted, filename)
        return None, summarize_image(file_bytes, doc.mime_type or "application/octet-stream", filename)

    try:
        extracted, summary = await asyncio.to_thread(process_file)
    except (GeminiUnavailableError, SecondaryLLMUnavailableError):
        logger.warning("Document summary synthesis is temporarily unavailable")
        await message.reply_text("Document analysis is temporarily unavailable. Please try again later.")
        return
    except Exception:
        logger.exception("Document processing failed for %s", filename)
        await message.reply_text("I hit a snag reading that file — could you try re-exporting it and sending again?")
        return

    def save_document() -> None:
        with get_session() as db:
            user, _ = get_or_create_user(
                db, telegram_id=update.effective_user.id,
                username=update.effective_user.username, first_name=update.effective_user.first_name,
            )
            record = Document(
                user_id=user.id, filename=filename, doc_type="pdf" if is_pdf else "other",
                extracted_text=extracted, summary=summary,
            )
            db.add(record)
            db.flush()
            user.last_document_id = record.id
            log_message(db, user, "user", f"[uploaded document: {filename}]", input_kind="document")
            log_message(db, user, "assistant", summary, intent="document_question")

    try:
        await asyncio.to_thread(save_document)
    except Exception:
        logger.exception("Could not persist processed document %s", filename)
        await message.reply_text("I read the file but couldn't save it safely. Please try again shortly.")
        return

    for chunk in chunk_for_telegram(f"📄 **{filename}**\n\n{summary}\n\n_Ask me anything else about this doc._"):
        await reply_with_html_fallback(message.reply_text, chunk)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message.photo:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    photo = message.photo[-1]  # highest resolution
    tg_file = await context.bot.get_file(photo.file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())

    def user_is_ready() -> bool:
        with get_session() as db:
            user, _ = get_or_create_user(
                db, telegram_id=update.effective_user.id,
                username=update.effective_user.username, first_name=update.effective_user.first_name,
            )
            return user.onboarding_stage == OnboardingStage.DONE.value

    if not await asyncio.to_thread(user_is_ready):
        await message.reply_text("Let's finish setting you up first — then send that image again!")
        return

    try:
        summary = await asyncio.to_thread(summarize_image, file_bytes, "image/jpeg", "photo")
    except GeminiUnavailableError:
        logger.warning("Image analysis is temporarily unavailable")
        await message.reply_text("Image analysis is temporarily unavailable. Please try again later.")
        return
    except Exception:
        logger.exception("Image processing failed")
        await message.reply_text("I couldn't read that image clearly — could you send a clearer shot?")
        return

    def save_photo() -> None:
        with get_session() as db:
            user, _ = get_or_create_user(
                db, telegram_id=update.effective_user.id,
                username=update.effective_user.username, first_name=update.effective_user.first_name,
            )
            record = Document(user_id=user.id, filename="photo", doc_type="image", extracted_text=None, summary=summary)
            db.add(record)
            db.flush()
            user.last_document_id = record.id
            log_message(db, user, "user", "[uploaded photo]", input_kind="image")
            log_message(db, user, "assistant", summary, intent="document_question")

    try:
        await asyncio.to_thread(save_photo)
    except Exception:
        logger.exception("Could not persist processed photo")
        await message.reply_text("I read the image but couldn't save it safely. Please try again shortly.")
        return

    for chunk in chunk_for_telegram(summary):
        await reply_with_html_fallback(message.reply_text, chunk)
