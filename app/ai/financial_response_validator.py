"""Reject unsupported numerical finance claims in model-generated responses."""
from __future__ import annotations

import re
from dataclasses import dataclass, field


NUMBER_RE = re.compile(r"(?<![A-Za-z])(?:[$€£₹]\s*)?[-+]?\d[\d,]*(?:\.\d+)?%?")
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")


@dataclass(slots=True)
class ValidationResult:
    valid: bool
    unsupported_claims: list[str] = field(default_factory=list)


def _parse(token: str) -> float | None:
    cleaned = re.sub(r"[$€£₹,%\s]", "", token)
    try:
        return float(cleaned)
    except ValueError:
        return None


def extract_numeric_values(text: str) -> list[float]:
    """Build an allowlist from retrieved text such as an uploaded filing."""
    output: list[float] = []
    for match in NUMBER_RE.finditer(text):
        value = _parse(match.group(0))
        if value is not None:
            output.append(value)
    return output


def validate_financial_response(text: str, allowed_numbers: list[float], tolerance: float = 0.011) -> ValidationResult:
    """Years and harmless list ordinals are ignored; all other numeric claims need grounding."""
    allowed = [float(item) for item in allowed_numbers]
    unsupported: list[str] = []
    for match in NUMBER_RE.finditer(text):
        token = match.group(0)
        value = _parse(token)
        if value is None or YEAR_RE.match(token.strip()):
            continue
        if any(abs(value - item) <= max(0.015, abs(item) * tolerance) for item in allowed):
            continue
        # Common formatting values derived from grounded decimals (e.g. 2.071 -> 2.07)
        if any(round(item, 2) == round(value, 2) for item in allowed):
            continue
        unsupported.append(token)
    return ValidationResult(valid=not unsupported, unsupported_claims=unsupported)
