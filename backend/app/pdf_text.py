from __future__ import annotations

from pathlib import Path

import pdfplumber

from .clinical_definitions import ClinicalDefinitionSet, load_clinical_definition_set
from .config import Settings
from .mistral_ocr import extract_pages_with_mistral
from .models import PageText
from .selection_extraction import extract_selection_entries_from_pdf_page


def extract_pages(
    path: Path,
    settings: Settings,
    definitions: ClinicalDefinitionSet | None = None,
) -> tuple[list[PageText], list[str]]:
    warnings: list[str] = []
    rule_set = definitions or load_clinical_definition_set()
    pages: list[PageText] = []
    selection_entries_by_page = {}

    with pdfplumber.open(path) as pdf:
        for index, page in enumerate(pdf.pages, start=1):
            text = page.extract_text() or ""
            selection_entries = extract_selection_entries_from_pdf_page(
                page,
                text,
                rule_set.selection_extraction,
            )
            selection_entries_by_page[index] = selection_entries
            pages.append(
                PageText(
                    page=index,
                    text=text,
                    provider="pdfplumber",
                    selection_entries=selection_entries,
                )
            )

    if settings.enable_mistral_ocr and settings.mistral_api_key:
        try:
            return (
                extract_pages_with_mistral(
                    path,
                    settings,
                    selection_configuration=rule_set.selection_extraction,
                    selection_entries_by_page=selection_entries_by_page,
                ),
                warnings,
            )
        except Exception as exc:  # pragma: no cover - fallback path depends on external OCR.
            warnings.append(f"Mistral OCR ist fehlgeschlagen; eingebetteter PDF-Text wird verwendet: {exc}")

    return pages, warnings
