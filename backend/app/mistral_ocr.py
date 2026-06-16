from __future__ import annotations

from pathlib import Path

from .config import Settings
from .models import PageText


def extract_pages_with_mistral(path: Path, settings: Settings) -> list[PageText]:
    """Run Mistral OCR when credentials are configured.

    The implementation uses the Mistral file-upload plus signed-url flow. If the
    SDK changes, the caller falls back to embedded PDF text and surfaces a
    warning in the analysis response.
    """

    try:
        from mistralai import Mistral
    except ImportError as exc:  # pragma: no cover - depends on optional package.
        raise RuntimeError("mistralai package is not installed") from exc

    if not settings.mistral_api_key:
        raise RuntimeError("MISTRAL_API_KEY is not configured")

    client = Mistral(api_key=settings.mistral_api_key)

    with path.open("rb") as file_obj:
        uploaded = client.files.upload(
            file={
                "file_name": path.name,
                "content": file_obj,
            },
            purpose="ocr",
        )

    signed_url = client.files.get_signed_url(file_id=uploaded.id)
    ocr_result = client.ocr.process(
        model=settings.mistral_ocr_model,
        document={
            "type": "document_url",
            "document_url": signed_url.url,
        },
    )

    pages: list[PageText] = []
    for index, page in enumerate(getattr(ocr_result, "pages", []), start=1):
        text = getattr(page, "markdown", None) or getattr(page, "text", "") or ""
        pages.append(PageText(page=index, text=text, provider="mistral_ocr"))

    if not pages:
        raise RuntimeError("Mistral OCR returned no pages")

    return pages

