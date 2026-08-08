from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ContextTypes

from app.ai.memory import log_message
from app.bot.handlers.common import chunk_for_telegram, get_or_create_user
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

    is_pdf = (doc.mime_type == "application/pdf") or doc.file_name.lower().endswith(".pdf")

    with get_session() as db:
        user, _ = get_or_create_user(
            db,
            telegram_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )

        if user.onboarding_stage != OnboardingStage.DONE.value:
            await message.reply_text("Let's finish setting you up first — I'll be able to dig into documents right after!")
            return

        try:
            if is_pdf:
                extracted = extract_pdf_text(file_bytes)
                summary = summarize_text(extracted, doc.file_name)
            else:
                extracted = None
                summary = summarize_image(file_bytes, doc.mime_type or "application/octet-stream", doc.file_name)
        except Exception:
            logger.exception("Document processing failed for %s", doc.file_name)
            await message.reply_text("I hit a snag reading that file — could you try re-exporting it and sending again?")
            return

        record = Document(
            user_id=user.id,
            filename=doc.file_name,
            doc_type="pdf" if is_pdf else "other",
            extracted_text=extracted,
            summary=summary,
        )
        db.add(record)
        db.flush()
        user.last_document_id = record.id

        log_message(db, user, "user", f"[uploaded document: {doc.file_name}]", input_kind="document")
        log_message(db, user, "assistant", summary, intent="document_qa")

    for chunk in chunk_for_telegram(f"📄 **{doc.file_name}**\n\n{summary}\n\n_Ask me anything else about this doc._"):
        try:
            await message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await message.reply_text(chunk)


async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if not message.photo:
        return

    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    photo = message.photo[-1]  # highest resolution
    tg_file = await context.bot.get_file(photo.file_id)
    file_bytes = bytes(await tg_file.download_as_bytearray())

    with get_session() as db:
        user, _ = get_or_create_user(
            db,
            telegram_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )
        if user.onboarding_stage != OnboardingStage.DONE.value:
            await message.reply_text("Let's finish setting you up first — then send that image again!")
            return

        try:
            summary = summarize_image(file_bytes, "image/jpeg", "photo")
        except Exception:
            logger.exception("Image processing failed")
            await message.reply_text("I couldn't read that image clearly — could you send a clearer shot?")
            return

        record = Document(user_id=user.id, filename="photo", doc_type="image", extracted_text=None, summary=summary)
        db.add(record)
        db.flush()
        user.last_document_id = record.id

        log_message(db, user, "user", "[uploaded photo]", input_kind="image")
        log_message(db, user, "assistant", summary, intent="document_qa")

    for chunk in chunk_for_telegram(summary):
        try:
            await message.reply_text(chunk, parse_mode="Markdown")
        except Exception:
            await message.reply_text(chunk)
