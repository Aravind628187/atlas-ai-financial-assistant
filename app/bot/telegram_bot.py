"""
Bot bootstrap. `/start` is the one unavoidable slash command Telegram
requires as an entry point — everything after that is plain conversation,
per the assignment's explicit "no commands, no menus" requirement.
"""
from __future__ import annotations

import logging

from telegram import Update
from telegram.error import NetworkError, RetryAfter
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

from app.ai.onboarding import start_onboarding
from app.bot.handlers.common import get_or_create_user
from app.bot.handlers.conversation import handle_text_message
from app.bot.handlers.documents import handle_document_message, handle_photo_message
from app.bot.handlers.voice import handle_voice_message
from app.bot.formatting import reply_with_html_fallback, send_with_html_fallback
from app.config import settings
from app.database import get_session
from app.models import OnboardingStage

logger = logging.getLogger("atlas.bot")


async def handle_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, (NetworkError, RetryAfter)):
        logger.warning("Telegram network temporarily unavailable; polling will retry automatically")
        return
    logger.error(
        "Unhandled Telegram update error: %s",
        error,
        exc_info=(type(error), error, error.__traceback__),
    )


async def handle_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    with get_session() as db:
        user, created = get_or_create_user(
            db,
            telegram_id=update.effective_user.id,
            username=update.effective_user.username,
            first_name=update.effective_user.first_name,
        )
        if user.onboarding_stage == OnboardingStage.NEW.value:
            user.onboarding_stage = OnboardingStage.ASKED_ROLE.value
            db.flush()
            greeting = start_onboarding()
        elif user.onboarding_stage != OnboardingStage.DONE.value:
            greeting = "Let's pick up where we left off — " + _resume_prompt(user.onboarding_stage)
        else:
            greeting = f"Welcome back{f', {user.first_name}' if user.first_name else ''}. What's on your radar today?"

    await reply_with_html_fallback(update.effective_message.reply_text, greeting)


def _resume_prompt(stage: str) -> str:
    prompts = {
        OnboardingStage.ASKED_ROLE.value: "what best describes you — investor, analyst, founder, or something else?",
        OnboardingStage.ASKED_INTERESTS.value: "which companies, sectors, or markets do you follow?",
        OnboardingStage.ASKED_MONITORING.value: "is there anything you'd like me to monitor?",
        OnboardingStage.ASKED_INTELLIGENCE.value: "which intelligence matters most — market news, earnings, SEC filings, or company research?",
        OnboardingStage.ASKED_BRIEFING_TIME.value: "what time do you want your daily briefing?",
        OnboardingStage.ASKED_INTEGRATIONS.value: "want to connect Gmail or Calendar, or skip for now?",
    }
    return prompts.get(stage, "let's continue.")


def build_application() -> Application:
    if not settings.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set. Add it to your .env file.")

    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .job_queue(None)  # Atlas owns the only scheduler; PTB's queue is unused.
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(20)
        .pool_timeout(10)
        .get_updates_connect_timeout(10)
        .get_updates_read_timeout(35)
        .get_updates_write_timeout(20)
        .get_updates_pool_timeout(10)
        .build()
    )

    application.add_handler(CommandHandler("start", handle_start))
    application.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice_message))
    application.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document_message))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_error_handler(handle_application_error)

    return application


async def send_message(application: Application, telegram_id: int, text: str) -> None:
    """Used by the scheduler to push proactive briefings/alerts."""
    try:
        await send_with_html_fallback(application.bot.send_message, chat_id=telegram_id, text=text)
    except NetworkError:
        logger.warning("Could not push Telegram message to user %s; network unavailable", telegram_id)
    except Exception:
        logger.exception("Failed to push proactive message to %s", telegram_id)
