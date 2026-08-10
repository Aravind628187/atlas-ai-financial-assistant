"""
Financial document intelligence: turns an uploaded PDF/image into an
executive summary, and answers free-text follow-up questions grounded in
the extracted text (simple but effective retrieval: the whole doc is
short enough for most earnings decks / term sheets to fit in one prompt;
for very long PDFs we chunk to the first N pages so latency stays sane).
"""
from __future__ import annotations

import io
import logging
import re

import pdfplumber

from app.ai.gemini_client import gemini
from app.ai.llm_gateway import llm_gateway
from app.ai.llm_gateway import SecondaryLLMUnavailableError
from app.ai.financial_response_validator import extract_numeric_values, validate_financial_response
from app.ai.prompts import DOCUMENT_SUMMARY_SYSTEM

logger = logging.getLogger("atlas.documents")

MAX_PDF_PAGES = 40
MAX_CHARS_FOR_PROMPT = 24000


def _extractive_fallback(extracted_text: str, question: str | None = None) -> str:
    """Return document text only; never synthesizes facts absent from the upload."""
    compact = re.sub(r"\s+", " ", extracted_text).strip()
    if not compact:
        return "I couldn't extract readable text from this document."
    sentences = re.split(r"(?<=[.!?])\s+", compact)
    if question:
        keywords = {word for word in re.findall(r"[a-zA-Z]{4,}", question.lower())}
        ranked = sorted(
            sentences, key=lambda sentence: sum(word in sentence.lower() for word in keywords), reverse=True,
        )
        selected = [sentence for sentence in ranked if any(word in sentence.lower() for word in keywords)][:3]
        if not selected:
            return "I couldn't locate that answer in the extracted document text."
        return "**Relevant document excerpts**\n\n" + "\n".join(f"• {sentence[:500]}" for sentence in selected)
    preview = " ".join(sentences[:5])[:1200]
    return f"**Document extracted — summary synthesis temporarily unavailable**\n\n{preview}"


def extract_pdf_text(file_bytes: bytes) -> str:
    text_parts = []
    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        for page in pdf.pages[:MAX_PDF_PAGES]:
            page_text = page.extract_text() or ""
            text_parts.append(page_text)
    return "\n".join(text_parts).strip()


def summarize_text(extracted_text: str, filename: str) -> str:
    truncated = extracted_text[:MAX_CHARS_FOR_PROMPT]
    prompt = f"Document: {filename}\n\n{truncated}"
    allowed = extract_numeric_values(truncated)
    try:
        response = llm_gateway.generate(prompt, system_instruction=DOCUMENT_SUMMARY_SYSTEM, temperature=0.3)
    except SecondaryLLMUnavailableError:
        return _extractive_fallback(truncated)
    verdict = validate_financial_response(response, allowed)
    if verdict.valid:
        return response
    logger.warning("Blocked document summary with unsupported numeric claims: %s", verdict.unsupported_claims[:8])
    strict_prompt = (
        f"Summarize this document using exact numbers only when copied from DOCUMENT. "
        f"Do not calculate or introduce examples.\n\nDOCUMENT: {filename}\n{truncated}"
    )
    try:
        retry = llm_gateway.generate(strict_prompt, system_instruction=DOCUMENT_SUMMARY_SYSTEM, temperature=0.1)
    except SecondaryLLMUnavailableError:
        return _extractive_fallback(truncated)
    if validate_financial_response(retry, allowed).valid:
        return retry
    return "I extracted the document, but I couldn't produce a summary without unsupported numerical claims. Ask a specific question and I'll answer only from the document."


def summarize_image(image_bytes: bytes, mime_type: str, filename: str) -> str:
    """For screenshots of charts/slides/reports where OCR-then-summarize loses layout context —
    send the image directly to Gemini's vision model instead."""
    prompt = (
        f"This image ({filename}) is a financial document/chart a user uploaded to Atlas. "
        "Give a tight executive summary: what it shows, the key numbers, and anything "
        "that looks like a notable change or risk. Under 150 words, Telegram-friendly formatting."
    )
    return gemini.analyze_image(image_bytes, mime_type, prompt)


def answer_question_about_document(extracted_text: str, question: str) -> str:
    truncated = extracted_text[:MAX_CHARS_FOR_PROMPT]
    prompt = (
        f"Using ONLY the document content below, answer the user's question concisely. "
        f"If the answer isn't in the document, say so plainly.\n\n"
        f"DOCUMENT:\n{truncated}\n\nQUESTION: {question}"
    )
    try:
        response = llm_gateway.generate(
            prompt,
            system_instruction="You are Atlas, a financial assistant answering questions about an uploaded document.",
            temperature=0.3,
        )
    except SecondaryLLMUnavailableError:
        return _extractive_fallback(truncated, question)
    verdict = validate_financial_response(response, extract_numeric_values(truncated))
    if verdict.valid:
        return response
    logger.warning("Blocked document answer with unsupported numeric claims: %s", verdict.unsupported_claims[:8])
    return "I couldn't verify the numerical claims needed to answer that from this document, so I won't guess."
