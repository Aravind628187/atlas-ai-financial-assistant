"""
Thin, resilient wrapper around the Gemini API.

Every call site in this project goes through this module so that:
  - the API key is read from settings exactly once
  - retries/backoff are handled in one place
  - text / vision / audio / JSON-mode calls share one code path
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger("atlas.gemini")

_configured = False


class GeminiUnavailableError(RuntimeError):
    """Expected temporary Gemini outage/quota state safe for user-facing fallbacks."""


def _is_retryable(exc: BaseException) -> bool:
    non_retryable = (
        GeminiUnavailableError,
        google_exceptions.ResourceExhausted,
        google_exceptions.TooManyRequests,
        google_exceptions.PermissionDenied,
        google_exceptions.Unauthenticated,
        google_exceptions.InvalidArgument,
    )
    return not isinstance(exc, non_retryable)


gemini_retry = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    retry=retry_if_exception(_is_retryable),
    reraise=True,
)


def _ensure_configured() -> None:
    global _configured
    if not _configured:
        if not settings.gemini_api_key:
            logger.warning("GEMINI_API_KEY is not set — AI calls will fail until it is configured.")
        else:
            genai.configure(api_key=settings.gemini_api_key)
        _configured = True


class GeminiClient:
    """Wraps a single Gemini model handle plus convenience helpers."""

    def __init__(self, model_name: str | None = None):
        _ensure_configured()
        self.model_name = model_name or settings.gemini_model
        self._model = genai.GenerativeModel(self.model_name)
        self._disabled_until = 0.0

    def _check_available(self) -> None:
        if not settings.gemini_api_key:
            raise GeminiUnavailableError("Gemini is not configured")
        remaining = self._disabled_until - time.monotonic()
        if remaining > 0:
            raise GeminiUnavailableError(f"Gemini cooling down for {remaining:.0f}s")

    def _mark_quota_exhausted(self) -> None:
        self._disabled_until = time.monotonic() + max(30, settings.gemini_cooldown_seconds)
        logger.warning("Gemini quota/rate limit reached; pausing AI calls for %ss", settings.gemini_cooldown_seconds)

    # ------------------------------------------------------------------
    # Core text generation (with optional chat history + system prompt)
    # ------------------------------------------------------------------
    @gemini_retry
    def generate(
        self,
        prompt: str,
        system_instruction: str | None = None,
        history: list[dict[str, str]] | None = None,
        temperature: float = 0.6,
        json_mode: bool = False,
    ) -> str:
        """
        history: list of {"role": "user"|"model", "content": str}
        """
        self._check_available()
        model = genai.GenerativeModel(self.model_name, system_instruction=system_instruction)
        generation_config: dict[str, Any] = {"temperature": temperature}
        if json_mode:
            generation_config["response_mime_type"] = "application/json"

        contents = []
        for turn in history or []:
            role = "model" if turn["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [turn["content"]]})
        contents.append({"role": "user", "parts": [prompt]})

        try:
            response = model.generate_content(contents, generation_config=generation_config)
        except (google_exceptions.ResourceExhausted, google_exceptions.TooManyRequests) as exc:
            self._mark_quota_exhausted()
            raise GeminiUnavailableError("Gemini quota is temporarily exhausted") from exc
        return (response.text or "").strip()

    # ------------------------------------------------------------------
    # Structured JSON extraction (used for intent routing / personalization)
    # ------------------------------------------------------------------
    def generate_json(
        self,
        prompt: str,
        system_instruction: str | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        raw = self.generate(prompt, system_instruction=system_instruction, temperature=temperature, json_mode=True)
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # Gemini occasionally wraps JSON in prose/fences despite json_mode; salvage it.
            start, end = raw.find("{"), raw.rfind("}")
            if start != -1 and end != -1:
                try:
                    return json.loads(raw[start : end + 1])
                except json.JSONDecodeError:
                    pass
            logger.error("Failed to parse JSON from Gemini response: %s", raw[:300])
            return {}

    # ------------------------------------------------------------------
    # Multimodal: images (charts, screenshots, document pages)
    # ------------------------------------------------------------------
    @gemini_retry
    def analyze_image(self, image_bytes: bytes, mime_type: str, prompt: str) -> str:
        self._check_available()
        try:
            response = self._model.generate_content([{"mime_type": mime_type, "data": image_bytes}, prompt])
        except (google_exceptions.ResourceExhausted, google_exceptions.TooManyRequests) as exc:
            self._mark_quota_exhausted()
            raise GeminiUnavailableError("Gemini quota is temporarily exhausted") from exc
        return (response.text or "").strip()

    # ------------------------------------------------------------------
    # Multimodal: voice notes (Gemini transcribes + understands directly)
    # ------------------------------------------------------------------
    @gemini_retry
    def transcribe_and_understand(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
        self._check_available()
        prompt = (
            "Transcribe this voice message from a finance professional talking to their "
            "AI financial assistant. Return ONLY the transcribed text, nothing else."
        )
        try:
            response = self._model.generate_content([{"mime_type": mime_type, "data": audio_bytes}, prompt])
        except (google_exceptions.ResourceExhausted, google_exceptions.TooManyRequests) as exc:
            self._mark_quota_exhausted()
            raise GeminiUnavailableError("Gemini quota is temporarily exhausted") from exc
        return (response.text or "").strip()


gemini = GeminiClient()
