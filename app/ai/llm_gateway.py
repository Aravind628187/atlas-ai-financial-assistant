"""Optional text-LLM failover with deterministic call-site fallbacks."""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol

import httpx

from app.ai.gemini_client import gemini
from app.config import settings
from app.services.runtime_state import reliability_telemetry


logger = logging.getLogger("atlas.llm_gateway")


class TextLLM(Protocol):
    def generate(self, prompt: str, **kwargs: Any) -> str: ...


class SecondaryLLMUnavailableError(RuntimeError):
    """A safe internal signal that no configured text model answered."""


class OpenAICompatibleSecondary:
    """Minimal OpenAI chat-completions adapter; created only when configured."""

    name = "openai"

    def __init__(self, api_key: str, model: str, client: httpx.Client | None = None) -> None:
        self.api_key = api_key
        self.model = model
        self.client = client or httpx.Client(timeout=settings.financial_provider_timeout_seconds)

    def generate(self, prompt: str, *, system_instruction: str | None = None,
                 history: list[dict[str, str]] | None = None, temperature: float = 0.6,
                 json_mode: bool = False, **_: Any) -> str:
        messages: list[dict[str, str]] = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        for turn in history or []:
            role = "assistant" if turn.get("role") == "assistant" else "user"
            messages.append({"role": role, "content": str(turn.get("content", ""))})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": temperature}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        response = self.client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
        return str(data["choices"][0]["message"]["content"]).strip()


def configured_secondary() -> TextLLM | None:
    provider = settings.secondary_llm_provider.strip().lower()
    if not provider or not settings.secondary_llm_api_key or not settings.secondary_llm_model:
        return None
    if provider in {"openai", "openai-compatible", "openai_compatible"}:
        return OpenAICompatibleSecondary(settings.secondary_llm_api_key, settings.secondary_llm_model)
    logger.warning("Unsupported secondary LLM provider configured; deterministic fallbacks remain active")
    return None


class LLMGateway:
    def __init__(self, primary: TextLLM | None = None, secondary: TextLLM | None = None) -> None:
        self.primary = primary or gemini
        self.secondary = secondary if secondary is not None else configured_secondary()

    def generate(self, prompt: str, **kwargs: Any) -> str:
        try:
            return self.primary.generate(prompt, **kwargs)
        except Exception as primary_error:
            reliability_telemetry.increment("llm_primary_failed")
            logger.info("Primary text synthesis unavailable: %s", type(primary_error).__name__)
        if self.secondary is not None:
            try:
                response = self.secondary.generate(prompt, **kwargs)
                reliability_telemetry.increment("llm_secondary_used")
                return response
            except Exception as secondary_error:
                logger.info("Secondary text synthesis unavailable: %s", type(secondary_error).__name__)
        reliability_telemetry.increment("deterministic_fallback_used")
        raise SecondaryLLMUnavailableError("No text synthesis provider is currently available")

    def generate_json(self, prompt: str, *, system_instruction: str | None = None,
                      temperature: float = 0.2) -> dict[str, Any]:
        try:
            generate_json = getattr(self.primary, "generate_json")
            value = generate_json(
                prompt, system_instruction=system_instruction, temperature=temperature,
            )
            return value if isinstance(value, dict) else {}
        except Exception as primary_error:
            reliability_telemetry.increment("llm_primary_failed")
            logger.info("Primary JSON synthesis unavailable: %s", type(primary_error).__name__)
        if self.secondary is None:
            reliability_telemetry.increment("deterministic_fallback_used")
            raise SecondaryLLMUnavailableError("No structured synthesis provider is currently available")
        try:
            raw = self.secondary.generate(
                prompt, system_instruction=system_instruction, temperature=temperature, json_mode=True,
            )
            reliability_telemetry.increment("llm_secondary_used")
        except Exception as secondary_error:
            logger.info("Secondary JSON synthesis unavailable: %s", type(secondary_error).__name__)
            reliability_telemetry.increment("deterministic_fallback_used")
            raise SecondaryLLMUnavailableError("No structured synthesis provider is currently available") from secondary_error
        if isinstance(raw, dict):
            return raw
        try:
            return json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            start, end = str(raw).find("{"), str(raw).rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(str(raw)[start:end + 1])
                except json.JSONDecodeError:
                    pass
        return {}


llm_gateway = LLMGateway()
