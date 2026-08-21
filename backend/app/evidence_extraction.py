from __future__ import annotations

import hashlib
import re
from typing import Any

from .billing_rules import candidate_gops_for_evidence_kind
from .billing_rule_store import get_runtime_clinical_definition_set
from .clinical_definitions import ClinicalDefinitionSet
from .clinical_rule_engine import (
    MatchContext,
    capture_value,
    condition_matches,
    normalize_text,
    render_value,
)
from .models import DocumentSegment, Evidence, ExcludedEvidence, PageText, ReviewCandidate


def extract_evidence(
    pages: list[PageText],
    segments: list[DocumentSegment],
    definitions: ClinicalDefinitionSet | None = None,
) -> tuple[list[Evidence], list[ReviewCandidate], list[ExcludedEvidence], dict[str, str | None]]:
    rule_set = definitions or get_runtime_clinical_definition_set()
    segment_type_by_page, relevant_pages = _segment_index(segments)
    evidence: list[Evidence] = []
    review: list[ReviewCandidate] = []
    excluded: list[ExcludedEvidence] = []
    case_context: dict[str, str | None] = {
        "treatment_start": None,
        "treatment_end": None,
        "quarter": None,
        "diagnosis": None,
    }
    datetime_state: dict[str, tuple[str | None, str | None]] = {}
    fallback_treatment_start: str | None = None

    for page in pages:
        segment_type = segment_type_by_page.get(page.page, str(rule_set.formats.get("fallback_segment_type") or "other"))
        segment_definition = rule_set.segment_types.get(segment_type, {})
        flags = {str(value) for value in segment_definition.get("flags") or []}
        datetimes = {
            role: _extract_datetime(page.text, definition, rule_set)
            for role, definition in rule_set.datetime_roles.items()
        }
        match_context = normalize_text(
            page.text or "",
            segment_type=segment_type,
            segment_flags=flags,
            datetimes=datetimes,
        )
        facts = {
            "page": page.page,
            "segment_type": segment_type,
        }

        _update_datetime_state(rule_set, match_context, datetime_state)
        _apply_context_updates(rule_set, match_context, case_context)

        page_evidence: list[Evidence] = []
        if page.page in relevant_pages:
            primary = segment_definition.get("primary_evidence")
            if isinstance(primary, dict) and condition_matches(primary.get("when") or {"always": True}, match_context):
                page_evidence.append(
                    _build_evidence(primary, page, match_context, datetimes, datetime_state, rule_set, facts)
                )

        deferred: list[dict[str, Any]] = []
        for rule in rule_set.evidence_rules:
            if rule.get("emit_if_no_page_evidence"):
                deferred.append(rule)
                continue
            if bool(rule.get("requires_relevant", True)) and page.page not in relevant_pages:
                continue
            item = _apply_evidence_rule(rule, page, match_context, datetimes, datetime_state, rule_set, facts)
            if item is None:
                continue
            page_evidence.append(item)
            _update_context_from_evidence(rule, item, case_context)
            also_review = rule.get("also_review")
            if isinstance(also_review, dict):
                review.append(_review_from_evidence(also_review, item))

        if not page_evidence:
            for rule in deferred:
                if bool(rule.get("requires_relevant", True)) and page.page not in relevant_pages:
                    continue
                item = _apply_evidence_rule(rule, page, match_context, datetimes, datetime_state, rule_set, facts)
                if item is not None:
                    page_evidence.append(item)

        for item in page_evidence:
            fallback_treatment_start = fallback_treatment_start or _join_datetime(item.service_date, item.service_time)
        evidence.extend(page_evidence)

        service_date, _ = datetimes.get(str(rule_set.formats.get("review_datetime_role") or "service"), (None, None))
        review.extend(_apply_review_rules(rule_set, page, match_context, service_date))
        excluded.extend(_apply_exclusion_rules(rule_set, page, match_context, service_date))

    if not case_context["treatment_start"]:
        case_context["treatment_start"] = fallback_treatment_start
    if case_context["treatment_start"]:
        case_context["quarter"] = quarter_from_date(case_context["treatment_start"][:10])
    else:
        service_dates = sorted(item.service_date for item in evidence if item.service_date)
        if service_dates:
            case_context["quarter"] = quarter_from_date(service_dates[0])

    return _dedupe_evidence(evidence), _dedupe_review(review), _dedupe_excluded(excluded), case_context


def quarter_from_date(date_value: str | None) -> str | None:
    if not date_value:
        return None
    year, month, _ = date_value.split("-")
    quarter = (int(month) - 1) // 3 + 1
    return f"{year}/Q{quarter}"


def _segment_index(segments: list[DocumentSegment]) -> tuple[dict[int, str], set[int]]:
    segment_type_by_page: dict[int, str] = {}
    relevant_pages: set[int] = set()
    for segment in segments:
        for page_no in range(segment.start_page, segment.end_page + 1):
            segment_type_by_page[page_no] = segment.segment_type
            if segment.relevant_for_billing:
                relevant_pages.add(page_no)
    return segment_type_by_page, relevant_pages


def _apply_evidence_rule(
    rule: dict[str, Any],
    page: PageText,
    context: MatchContext,
    datetimes: dict[str, tuple[str | None, str | None]],
    datetime_state: dict[str, tuple[str | None, str | None]],
    definitions: ClinicalDefinitionSet,
    facts: dict[str, Any],
) -> Evidence | None:
    if not condition_matches(rule["when"], context):
        return None
    unless = rule.get("unless")
    if isinstance(unless, dict) and condition_matches(unless, context):
        return None

    capture = rule.get("capture")
    value = capture_value(capture, context) if isinstance(capture, dict) else None
    if capture and not value:
        return None
    return _build_evidence(rule, page, context, datetimes, datetime_state, definitions, {**facts, "value": value or ""})


def _build_evidence(
    rule: dict[str, Any],
    page: PageText,
    context: MatchContext,
    datetimes: dict[str, tuple[str | None, str | None]],
    datetime_state: dict[str, tuple[str | None, str | None]],
    definitions: ClinicalDefinitionSet,
    variables: dict[str, Any],
) -> Evidence:
    service_date, service_time, carried = _resolve_rule_datetime(
        rule,
        context,
        datetimes,
        datetime_state,
        definitions,
    )
    rendered = dict(rule)
    for field in ("kind", "label", "text", "value", "unit", "metadata", "search_terms"):
        if field in rule:
            rendered[field] = render_value(rule[field], variables)
    text_value = page.text if rendered.get("text_mode", "source") == "source" else str(rendered.get("text") or "")
    metadata = dict(rendered.get("metadata") or {})
    search_terms = rendered.get("search_terms") or []
    if search_terms:
        metadata["search_terms"] = list(search_terms)
    if carried:
        metadata["service_datetime_carried_from_previous_page"] = True
    value = variables.get("value") or rendered.get("value")
    return _ev(
        kind=str(rendered["kind"]),
        label=str(rendered["label"]),
        page=page.page,
        text=text_value,
        service_date=service_date,
        service_time=service_time,
        confidence=float(rendered.get("confidence") or 0.8),
        value=str(value) if value else None,
        unit=str(rendered.get("unit")) if rendered.get("unit") else None,
        metadata=metadata,
    )


def _resolve_rule_datetime(
    rule: dict[str, Any],
    context: MatchContext,
    datetimes: dict[str, tuple[str | None, str | None]],
    datetime_state: dict[str, tuple[str | None, str | None]],
    definitions: ClinicalDefinitionSet,
) -> tuple[str | None, str | None, bool]:
    procedure_patterns = rule.get("datetime_match_patterns")
    if isinstance(procedure_patterns, list) and procedure_patterns:
        value = _extract_procedure_datetime(context, [str(item) for item in procedure_patterns], definitions)
        if value[0]:
            return value[0], value[1], False

    segment_sources = (definitions.formats.get("segment_datetime_sources") or {}).get(context.segment_type or "")
    sources = rule.get("datetime_sources") or segment_sources or definitions.formats.get("default_datetime_sources") or ["service"]
    for source in sources:
        source_name = str(source)
        if source_name.startswith("state:"):
            value = datetime_state.get(source_name.split(":", 1)[1], (None, None))
            if value[0]:
                return value[0], value[1], True
            continue
        value = datetimes.get(source_name, (None, None))
        if value[0]:
            return value[0], value[1], False
    return None, None, False


def _extract_procedure_datetime(
    context: MatchContext,
    procedure_patterns: list[str],
    definitions: ClinicalDefinitionSet,
) -> tuple[str | None, str | None]:
    source_name = str(definitions.formats.get("procedure_datetime_source") or "key")
    source = context.source(source_name)
    procedure = "(?:" + "|".join(procedure_patterns) + ")"
    for suffix in definitions.formats.get("procedure_datetime_patterns") or []:
        pattern = procedure + str(suffix)
        matches = list(re.finditer(pattern, source, re.IGNORECASE))
        if not matches:
            continue
        match = matches[-1]
        date_value = _date_to_iso(match.group(1))
        time_value = match.group(2) if match.lastindex and match.lastindex >= 2 else None
        return date_value, time_value
    return None, None


def _extract_datetime(
    text: str,
    definition: dict[str, Any],
    definitions: ClinicalDefinitionSet,
) -> tuple[str | None, str | None]:
    normalized = normalize_text(text)
    for pattern_definition in definition.get("patterns") or []:
        source = normalized.source(str(pattern_definition.get("source") or "compact"))
        matches = list(re.finditer(str(pattern_definition["regex"]), source, re.IGNORECASE))
        if not matches:
            continue
        selected = matches[-1] if pattern_definition.get("select") == "last" else matches[0]
        date_group = int(pattern_definition.get("date_group") or 1)
        time_group = int(pattern_definition.get("time_group") or 2)
        date_value = _date_to_iso(selected.group(date_group))
        time_value = selected.group(time_group) if selected.lastindex and selected.lastindex >= time_group else None
        return date_value, time_value

    if not definition.get("fallback_first"):
        return None, None
    date_regex = str(definitions.formats.get("date_regex") or r"(\d{2}\.\d{2}\.\d{4})")
    time_regex = str(definitions.formats.get("time_regex") or r"(\d{2}:\d{2})")
    excluded = [str(value) for value in definition.get("fallback_excluded_before") or []]
    excluded_regex = [str(value) for value in definition.get("fallback_excluded_before_regex") or []]
    exclusion_window = int(definition.get("fallback_exclusion_window") or 80)
    date_value = _first_allowed_match(normalized.folded, date_regex, excluded, excluded_regex, exclusion_window)
    time_value = _first_allowed_match(normalized.folded, time_regex, excluded, excluded_regex, exclusion_window)
    return _date_to_iso(date_value), time_value


def _first_allowed_match(
    text: str,
    pattern: str,
    excluded_before: list[str],
    excluded_before_regex: list[str],
    exclusion_window: int,
) -> str | None:
    for match in re.finditer(pattern, text, re.IGNORECASE):
        before = re.sub(r"[^a-z0-9]+", "", text[max(0, match.start() - 80) : match.start()].casefold())
        recent = before[-exclusion_window:]
        if any(re.sub(r"[^a-z0-9]+", "", value.casefold()) in recent for value in excluded_before):
            continue
        if any(re.search(pattern, recent, re.IGNORECASE) for pattern in excluded_before_regex):
            continue
        return match.group(1)
    return None


def _date_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    day, month, year = value.split(".")
    return f"{year}-{month}-{day}"


def _update_datetime_state(
    definitions: ClinicalDefinitionSet,
    context: MatchContext,
    state: dict[str, tuple[str | None, str | None]],
) -> None:
    for track in definitions.state_tracks:
        if not condition_matches(track["when"], context):
            continue
        role = str(track["datetime_role"])
        value = (context.datetimes or {}).get(role, (None, None))
        if value[0]:
            state[str(track["name"])] = value


def _apply_context_updates(
    definitions: ClinicalDefinitionSet,
    context: MatchContext,
    case_context: dict[str, str | None],
) -> None:
    for update in definitions.context_updates:
        if not condition_matches(update["when"], context):
            continue
        key = str(update["key"])
        role = str(update["datetime_role"])
        date_value, time_value = (context.datetimes or {}).get(role, (None, None))
        value = _join_datetime(date_value, time_value)
        if value and (update.get("replace") or not case_context.get(key)):
            case_context[key] = value


def _update_context_from_evidence(
    rule: dict[str, Any],
    evidence: Evidence,
    case_context: dict[str, str | None],
) -> None:
    update = rule.get("context_update")
    if not isinstance(update, dict):
        return
    key = str(update["key"])
    source = str(update.get("source") or "datetime")
    value = evidence.value if source == "value" else _join_datetime(evidence.service_date, evidence.service_time)
    if value and (update.get("replace") or not case_context.get(key)):
        case_context[key] = value


def _apply_review_rules(
    definitions: ClinicalDefinitionSet,
    page: PageText,
    context: MatchContext,
    service_date: str | None,
) -> list[ReviewCandidate]:
    quarter = quarter_from_date(service_date)
    result: list[ReviewCandidate] = []
    for rule in definitions.review_rules:
        if not condition_matches(rule["when"], context):
            continue
        candidate_kind = str(rule.get("candidate_kind") or "")
        result.append(
            ReviewCandidate(
                evidence=str(rule["evidence"]),
                evidence_pages=[page.page],
                reason=str(rule["reason"]),
                possible_gops=candidate_gops_for_evidence_kind(candidate_kind, quarter=quarter) if candidate_kind else [],
            )
        )
    return result


def _review_from_evidence(definition: dict[str, Any], evidence: Evidence) -> ReviewCandidate:
    candidate_kind = str(definition.get("candidate_kind") or evidence.kind)
    return ReviewCandidate(
        evidence=str(definition.get("evidence") or evidence.label),
        evidence_pages=[evidence.page],
        reason=str(definition["reason"]),
        possible_gops=candidate_gops_for_evidence_kind(candidate_kind, quarter=quarter_from_date(evidence.service_date)),
    )


def _apply_exclusion_rules(
    definitions: ClinicalDefinitionSet,
    page: PageText,
    context: MatchContext,
    service_date: str | None,
) -> list[ExcludedEvidence]:
    quarter = quarter_from_date(service_date)
    result: list[ExcludedEvidence] = []
    for rule in definitions.exclusion_rules:
        if not condition_matches(rule["when"], context):
            continue
        candidate_kind = str(rule.get("candidate_kind") or "")
        candidates = candidate_gops_for_evidence_kind(candidate_kind, quarter=quarter) if candidate_kind else []
        result.append(
            ExcludedEvidence(
                evidence=str(rule["evidence"]),
                evidence_pages=[page.page],
                reason=str(rule["reason"]),
                not_billed_gop=candidates[0] if candidates else None,
            )
        )
    return result


def _ev(
    kind: str,
    label: str,
    page: int,
    text: str,
    service_date: str | None,
    service_time: str | None,
    confidence: float,
    value: str | None = None,
    unit: str | None = None,
    metadata: dict[str, object] | None = None,
) -> Evidence:
    snippet = re.sub(r"\s+", " ", text).strip()[:240]
    digest = hashlib.sha1(f"{kind}:{page}:{snippet}".encode("utf-8")).hexdigest()[:10]
    return Evidence(
        evidence_id=f"ev-{digest}",
        kind=kind,
        label=label,
        page=page,
        service_date=service_date,
        service_time=service_time,
        value=value,
        unit=unit,
        text=snippet,
        confidence=confidence,
        metadata=metadata or {},
    )


def _join_datetime(date_value: str | None, time_value: str | None) -> str | None:
    if not date_value:
        return None
    return f"{date_value}T{time_value or '00:00'}:00"


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, int]] = set()
    result: list[Evidence] = []
    for item in items:
        key = (item.kind, item.page)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def _dedupe_review(items: list[ReviewCandidate]) -> list[ReviewCandidate]:
    merged: dict[tuple[str, str], ReviewCandidate] = {}
    for item in items:
        key = (item.evidence, item.reason)
        if key not in merged:
            merged[key] = item
        else:
            merged[key].evidence_pages = sorted(set(merged[key].evidence_pages + item.evidence_pages))
    return list(merged.values())


def _dedupe_excluded(items: list[ExcludedEvidence]) -> list[ExcludedEvidence]:
    merged: dict[tuple[str, str], ExcludedEvidence] = {}
    for item in items:
        key = (item.evidence, item.reason)
        if key not in merged:
            merged[key] = item
        else:
            merged[key].evidence_pages = sorted(set(merged[key].evidence_pages + item.evidence_pages))
    return list(merged.values())
