from __future__ import annotations

import re

from .models import DocumentSegment, PageText


SEGMENT_LABELS = {
    "case_context": "Fallkontext / ZNA",
    "treatment_report": "Behandlungsbericht",
    "radiology_report": "Radiologiebefund",
    "laboratory_result": "Laborbefund",
    "consult": "Konsil",
    "ecg": "EKG",
    "data_capture": "Datenerfassung",
    "request": "Anforderung / Indikationspruefung",
    "other": "Sonstiges Dokument",
}

RELEVANT_TYPES = {"case_context", "treatment_report", "radiology_report", "laboratory_result", "ecg"}


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", text.lower())


def classify_page(text: str) -> tuple[str, float, list[str]]:
    lower = text.lower()
    compact = _compact(text)
    reasons: list[str] = []

    if "laborbefund" in lower or ("untersuchung" in lower and "referenzbereich" in lower):
        reasons.append("Laborbefund-Marker gefunden")
        return "laboratory_result", 0.95, reasons
    if "radiologie - befund" in lower or ("befund" in lower and "ctkopf" in compact):
        reasons.append("Radiologiebefund-Marker gefunden")
        return "radiology_report", 0.95, reasons
    if (
        "ambulanzaugen-anforderung" in compact
        or "status:angefordert" in compact
        or ("anforderung" in lower and "auftragsdatum" in lower)
        or "indikationspr" in lower
        or "angeforderte untersuchungen" in lower
    ):
        reasons.append("Anforderungs-/Indikationsmarker gefunden")
        return "request", 0.72, reasons
    if (
        "ambulanzaugen-befund" in compact
        or "notfall-symptomorientierteuntersuchung" in compact
        or ("notfallambulanzaugenklinik" in compact and "befund" in lower)
        or ("augenambulanz" in compact and ("anamnese" in lower or "beurteilung" in lower))
        or "vordereraugenabschnitt" in compact
        or "hintereraugenabschnitt" in compact
        or ("beurteilung:" in lower and "therapie" in lower)
        or ("diagnose:" in lower and "herpeskeratitis" in compact)
        or "wirberichtenihnen" in compact and "augenambulanz" in compact
    ):
        reasons.append("Augenambulanz-/Fachambulanz-Befund gefunden")
        return "treatment_report", 0.86, reasons
    if "behandlungsbericht zna" in lower or "diagnostik:" in lower and "aufnahme" in lower:
        reasons.append("Behandlungsbericht/ZNA-Marker gefunden")
        return "treatment_report", 0.88, reasons
    if "aufnahmezna" in compact or "kv-abrechnung/notfalldienst" in compact:
        reasons.append("ZNA-/KV-Notfall-Kontext gefunden")
        return "case_context", 0.9, reasons
    if "konsil - befund" in lower or "konsil" in lower and "durchgef" in lower:
        reasons.append("Konsil-Marker gefunden")
        return "consult", 0.82, reasons
    if "standard 12 ableitungen" in lower or "ekg" in lower or "sinusrhythmus" in lower:
        reasons.append("EKG-Marker gefunden")
        return "ecg", 0.78, reasons
    if "datenerfassung" in lower:
        reasons.append("Datenerfassung-Marker gefunden")
        return "data_capture", 0.78, reasons
    return "other", 0.5, ["kein spezifischer Marker"]


def segment_pages(pages: list[PageText]) -> list[DocumentSegment]:
    page_classes = [(page.page, *classify_page(page.text)) for page in pages]
    if not page_classes:
        return []

    segments: list[DocumentSegment] = []
    current_type = page_classes[0][1]
    start_page = page_classes[0][0]
    end_page = start_page
    confidences = [page_classes[0][2]]
    reasons = list(page_classes[0][3])

    for page_no, segment_type, confidence, page_reasons in page_classes[1:]:
        if segment_type == current_type and page_no == end_page + 1:
            end_page = page_no
            confidences.append(confidence)
            reasons.extend(page_reasons)
            continue

        segments.append(_make_segment(len(segments) + 1, current_type, start_page, end_page, confidences, reasons))
        current_type = segment_type
        start_page = page_no
        end_page = page_no
        confidences = [confidence]
        reasons = list(page_reasons)

    segments.append(_make_segment(len(segments) + 1, current_type, start_page, end_page, confidences, reasons))
    return segments


def _make_segment(
    index: int,
    segment_type: str,
    start_page: int,
    end_page: int,
    confidences: list[float],
    reasons: list[str],
) -> DocumentSegment:
    unique_reasons = list(dict.fromkeys(reasons))
    return DocumentSegment(
        segment_id=f"seg-{index:03d}",
        segment_type=segment_type,
        title=SEGMENT_LABELS.get(segment_type, segment_type),
        start_page=start_page,
        end_page=end_page,
        relevant_for_billing=segment_type in RELEVANT_TYPES,
        confidence=round(sum(confidences) / max(len(confidences), 1), 2),
        reasons=unique_reasons[:5],
    )
