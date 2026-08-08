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
from typing import Any

import google.generativeai as genai
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings

logger = logging.getLogger("atlas.gemini")

_configured = False


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

    # ------------------------------------------------------------------
    # Core text generation (with optional chat history + system prompt)
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
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
        model = genai.GenerativeModel(self.model_name, system_instruction=system_instruction)
        generation_config: dict[str, Any] = {"temperature": temperature}
        if json_mode:
            generation_config["response_mime_type"] = "application/json"

        contents = []
        for turn in history or []:
            role = "model" if turn["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [turn["content"]]})
        contents.append({"role": "user", "parts": [prompt]})

        response = model.generate_content(contents, generation_config=generation_config)
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
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def analyze_image(self, image_bytes: bytes, mime_type: str, prompt: str) -> str:
        response = self._model.generate_content(
            [{"mime_type": mime_type, "data": image_bytes}, prompt]
        )
        return (response.text or "").strip()

    # ------------------------------------------------------------------
    # Multimodal: voice notes (Gemini transcribes + understands directly)
    # ------------------------------------------------------------------
    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8))
    def transcribe_and_understand(self, audio_bytes: bytes, mime_type: str = "audio/ogg") -> str:
        prompt = (
            "Transcribe this voice message from a finance professional talking to their "
            "AI financial assistant. Return ONLY the transcribed text, nothing else."
        )
        response = self._model.generate_content(
            [{"mime_type": mime_type, "data": audio_bytes}, prompt]
        )
        return (response.text or "").strip()


gemini = GeminiClient()
