from __future__ import annotations

import re
from typing import Any, Iterable

from .models import SelectionEntry


def extract_selection_entries_from_text(
    text: str,
    configuration: dict[str, Any],
) -> list[SelectionEntry]:
    row_pattern = _row_pattern(configuration)
    if row_pattern is None:
        return []

    entries: list[SelectionEntry] = []
    context_terms = [str(value).casefold() for value in configuration.get("list_context_terms") or []]
    has_context = any(term in text.casefold() for term in context_terms)
    for line in text.splitlines():
        marker_found = False
        for marker in configuration.get("text_markers") or []:
            pattern = str(marker.get("regex") or "")
            state = str(marker.get("state") or "")
            if state not in {"checked", "unchecked", "ambiguous"} or not pattern:
                continue
            match = re.search(pattern, line, re.IGNORECASE)
            if not match:
                continue
            marker_found = True
            parsed = _parse_row(line[match.end() :], row_pattern, configuration)
            if parsed is None:
                break
            code, label, quantity = parsed
            entries.append(
                SelectionEntry(
                    code=code,
                    label=label,
                    quantity=quantity,
                    state=state,
                    confidence=float(marker.get("confidence") or 0.8),
                    source="ocr_text",
                )
            )
            break
        if marker_found or not has_context:
            continue
        unmarked_state = str(configuration.get("unmarked_rows_state") or "")
        if unmarked_state not in {"checked", "unchecked", "ambiguous"}:
            continue
        parsed = _parse_row(line, row_pattern, configuration)
        if parsed is None:
            continue
        code, label, quantity = parsed
        entries.append(
            SelectionEntry(
                code=code,
                label=label,
                quantity=quantity,
                state=unmarked_state,
                confidence=float(configuration.get("unmarked_rows_confidence") or 0.45),
                source="ocr_text",
            )
        )
    return merge_selection_entries(entries)


def extract_selection_entries_from_pdf_page(
    page: Any,
    text: str,
    configuration: dict[str, Any],
) -> list[SelectionEntry]:
    row_pattern = _row_pattern(configuration)
    vector = configuration.get("pdf_vector") or {}
    if row_pattern is None or not isinstance(vector, dict):
        return []

    words = list(page.extract_words(x_tolerance=1, y_tolerance=2, keep_blank_chars=False) or [])
    candidates = _vector_candidates(page, vector)
    row_tolerance = float(vector.get("row_vertical_tolerance") or 8.0)
    text_gap = float(vector.get("row_text_start_gap") or 2.0)
    parsed: list[tuple[SelectionEntry, float]] = []

    for state, confidence, bbox in candidates:
        x0, top, x1, bottom = bbox
        center = (top + bottom) / 2
        row_words = [
            word
            for word in words
            if float(word.get("x0") or 0) >= x1 + text_gap
            and abs(_word_center(word) - center) <= row_tolerance
        ]
        row_text = " ".join(str(word.get("text") or "") for word in sorted(row_words, key=lambda item: item["x0"]))
        row = _parse_row(row_text, row_pattern, configuration)
        if row is None:
            continue
        code, label, quantity = row
        parsed.append(
            (
                SelectionEntry(
                    code=code,
                    label=label,
                    quantity=quantity,
                    state=state,
                    confidence=confidence,
                    source="pdf_vector",
                    bbox=bbox,
                ),
                x0,
            )
        )

    if not parsed:
        return []

    column_tolerance = float(vector.get("column_tolerance") or 2.0)
    minimum_rows = int(vector.get("minimum_rows_in_column") or 2)
    context_terms = [str(value).casefold() for value in configuration.get("list_context_terms") or []]
    has_context = any(term in text.casefold() for term in context_terms)
    entries = [
        entry
        for entry, x0 in parsed
        if has_context or sum(abs(other_x0 - x0) <= column_tolerance for _, other_x0 in parsed) >= minimum_rows
    ]
    return merge_selection_entries(entries)


def merge_selection_entries(*groups: Iterable[SelectionEntry]) -> list[SelectionEntry]:
    grouped: dict[str, list[SelectionEntry]] = {}
    order: list[str] = []
    for group in groups:
        for entry in group:
            key = entry.code.casefold()
            if key not in grouped:
                grouped[key] = []
                order.append(key)
            grouped[key].append(entry)

    result: list[SelectionEntry] = []
    for key in order:
        candidates = grouped[key]
        vector_candidates = [entry for entry in candidates if entry.source == "pdf_vector"]
        authoritative = vector_candidates or candidates
        states = {entry.state for entry in authoritative}
        best = max(authoritative, key=lambda entry: entry.confidence)
        state = next(iter(states)) if len(states) == 1 else "ambiguous"
        sources = {entry.source for entry in candidates}
        result.append(
            SelectionEntry(
                code=best.code,
                label=next((entry.label for entry in candidates if entry.label), None),
                quantity=next((entry.quantity for entry in candidates if entry.quantity is not None), None),
                state=state,
                confidence=min(best.confidence, 0.7) if state == "ambiguous" else best.confidence,
                source=next(iter(sources)) if len(sources) == 1 else "merged",
                bbox=next((entry.bbox for entry in vector_candidates if entry.bbox), None),
            )
        )
    return result


def _row_pattern(configuration: dict[str, Any]) -> re.Pattern[str] | None:
    pattern = str(configuration.get("row_regex") or "")
    if not pattern:
        return None
    return re.compile(pattern)


def _parse_row(
    text: str,
    row_pattern: re.Pattern[str],
    configuration: dict[str, Any],
) -> tuple[str, str | None, float | None] | None:
    match = row_pattern.search(text.strip())
    if not match:
        return None
    code = str(match.groupdict().get("code") or "").strip()
    if not code:
        return None
    quantity_text = str(match.groupdict().get("quantity") or "").strip()
    standalone_pattern = str(configuration.get("standalone_code_regex") or "")
    if not quantity_text and standalone_pattern and not re.search(standalone_pattern, code):
        return None
    quantity = float(quantity_text.replace(",", ".")) if quantity_text else None
    label = re.sub(r"\s+", " ", str(match.groupdict().get("label") or "")).strip() or None
    return code.upper(), label, quantity


def _vector_candidates(
    page: Any,
    configuration: dict[str, Any],
) -> list[tuple[str, float, tuple[float, float, float, float]]]:
    candidates: dict[tuple[float, float, float, float], tuple[str, float, tuple[float, float, float, float]]] = {}
    unchecked_confidence = float(configuration.get("unchecked_confidence") or 0.98)
    checked_confidence = float(configuration.get("checked_confidence") or 0.99)

    for rectangle in getattr(page, "rects", []) or []:
        bbox = _square_bbox(rectangle, configuration)
        if bbox is not None:
            candidates[_bbox_key(bbox)] = ("unchecked", unchecked_confidence, bbox)

    for shape in getattr(page, "curves", []) or []:
        bbox = _square_bbox(shape, configuration)
        if bbox is not None and _has_diagonal(shape, bbox, configuration):
            candidates[_bbox_key(bbox)] = ("checked", checked_confidence, bbox)

    for line in getattr(page, "lines", []) or []:
        bbox = _square_bbox(line, configuration)
        if bbox is not None and _has_diagonal(line, bbox, configuration):
            candidates[_bbox_key(bbox)] = ("checked", checked_confidence, bbox)

    return list(candidates.values())


def _square_bbox(
    shape: dict[str, Any],
    configuration: dict[str, Any],
) -> tuple[float, float, float, float] | None:
    try:
        x0 = float(shape["x0"])
        x1 = float(shape["x1"])
        top = float(shape["top"])
        bottom = float(shape["bottom"])
        width = abs(float(shape["width"])) if shape.get("width") is not None else abs(x1 - x0)
        height = abs(float(shape["height"])) if shape.get("height") is not None else abs(bottom - top)
    except (KeyError, TypeError, ValueError):
        return None
    minimum = float(configuration.get("minimum_size") or 4.0)
    maximum = float(configuration.get("maximum_size") or 18.0)
    tolerance = float(configuration.get("aspect_ratio_tolerance") or 0.3)
    if not (minimum <= width <= maximum and minimum <= height <= maximum):
        return None
    if abs(width - height) / max(width, height) > tolerance:
        return None
    return min(x0, x1), min(top, bottom), max(x0, x1), max(top, bottom)


def _has_diagonal(
    shape: dict[str, Any],
    bbox: tuple[float, float, float, float],
    configuration: dict[str, Any],
) -> bool:
    x0, top, x1, bottom = bbox
    threshold = float(configuration.get("diagonal_span_ratio") or 0.55)
    points = shape.get("pts")
    if isinstance(points, list) and len(points) >= 2:
        for first, second in zip(points, points[1:]):
            if len(first) < 2 or len(second) < 2:
                continue
            if abs(float(second[0]) - float(first[0])) >= (x1 - x0) * threshold and abs(
                float(second[1]) - float(first[1])
            ) >= (bottom - top) * threshold:
                return True
        return False
    try:
        return abs(float(shape["x1"]) - float(shape["x0"])) >= (x1 - x0) * threshold and abs(
            float(shape["bottom"]) - float(shape["top"])
        ) >= (bottom - top) * threshold
    except (KeyError, TypeError, ValueError):
        return False


def _bbox_key(bbox: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    return tuple(round(value, 1) for value in bbox)


def _word_center(word: dict[str, Any]) -> float:
    return (float(word.get("top") or 0) + float(word.get("bottom") or 0)) / 2
