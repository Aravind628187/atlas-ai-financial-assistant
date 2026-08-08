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

import pdfplumber

from app.ai.gemini_client import gemini
from app.ai.prompts import DOCUMENT_SUMMARY_SYSTEM

logger = logging.getLogger("atlas.documents")

MAX_PDF_PAGES = 40
MAX_CHARS_FOR_PROMPT = 24000


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
    return gemini.generate(prompt, system_instruction=DOCUMENT_SUMMARY_SYSTEM, temperature=0.3)


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
    return gemini.generate(
        prompt,
        system_instruction="You are Atlas, a financial assistant answering questions about an uploaded document.",
        temperature=0.3,
    )
