from __future__ import annotations

from .billing_rule_store import get_runtime_clinical_definition_set
from .clinical_definitions import ClinicalDefinitionSet
from .clinical_rule_engine import condition_matches, normalize_text
from .models import DocumentSegment, PageText


def classify_page(
    text: str,
    definitions: ClinicalDefinitionSet | None = None,
) -> tuple[str, float, list[str]]:
    rule_set = definitions or get_runtime_clinical_definition_set()
    context = normalize_text(text)
    for rule in rule_set.segment_classifiers:
        if condition_matches(rule["when"], context):
            return (
                str(rule["segment_type"]),
                float(rule.get("confidence") or 0.5),
                [str(rule.get("reason") or rule["rule_id"])],
            )
    fallback = str(rule_set.formats.get("fallback_segment_type") or "other")
    return fallback, 0.5, [str(rule_set.formats.get("fallback_reason") or "Keine Klassifikationsregel erfüllt")]


def segment_pages(
    pages: list[PageText],
    definitions: ClinicalDefinitionSet | None = None,
) -> list[DocumentSegment]:
    rule_set = definitions or get_runtime_clinical_definition_set()
    page_classes = [(page.page, *classify_page(page.text, rule_set)) for page in pages]
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

        segments.append(
            _make_segment(rule_set, len(segments) + 1, current_type, start_page, end_page, confidences, reasons)
        )
        current_type = segment_type
        start_page = page_no
        end_page = page_no
        confidences = [confidence]
        reasons = list(page_reasons)

    segments.append(
        _make_segment(rule_set, len(segments) + 1, current_type, start_page, end_page, confidences, reasons)
    )
    return segments


def _make_segment(
    definitions: ClinicalDefinitionSet,
    index: int,
    segment_type: str,
    start_page: int,
    end_page: int,
    confidences: list[float],
    reasons: list[str],
) -> DocumentSegment:
    segment_definition = definitions.segment_types.get(segment_type, {})
    flags = {str(flag) for flag in segment_definition.get("flags") or []}
    unique_reasons = list(dict.fromkeys(reasons))
    return DocumentSegment(
        segment_id=f"seg-{index:03d}",
        segment_type=segment_type,
        title=str(segment_definition.get("label") or segment_type),
        start_page=start_page,
        end_page=end_page,
        relevant_for_billing="billing_relevant" in flags,
        confidence=round(sum(confidences) / max(len(confidences), 1), 2),
        reasons=unique_reasons[:5],
    )
