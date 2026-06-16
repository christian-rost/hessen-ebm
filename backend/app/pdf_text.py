from __future__ import annotations

from pathlib import Path

import pdfplumber

from .config import Settings
from .mistral_ocr import extract_pages_with_mistral
from .models import PageText


def extract_pages(path: Path, settings: Settings) -> tuple[list[PageText], list[str]]:
    warnings: list[str] = []

    if settings.enable_mistral_ocr and settings.mistral_api_key:
        try:
            return extract_pages_with_mistral(path, settings), warnings
        except Exception as exc:  # pragma: no cover - fallback path depends on external OCR.
            warnings.append(f"Mistral OCR failed, using embedded PDF text: {exc}")

    pages: list[PageText] = []
    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            pages.append(PageText(page=index, text=text, provider="pdfplumber"))

    return pages, warnings

