from __future__ import annotations

from datetime import datetime

from .billing_rule_definitions import BillingRuleSet, DerivedRuleDefinition, definition_is_applicable
from .billing_rule_store import get_runtime_billing_rule_set
from .billing_rules import (
    BillingRuleContext,
    DerivedGopDecision,
    billing_rule_guidance,
    derive_additional_gops,
    evidence_billing_rules,
    evaluate_catalog_context_rules,
    resolve_evidence_rule_gop,
)
from .billing_events import (
    build_billing_events,
    events_for_evidence_kind,
    finalize_billing_timeline,
    primary_episode_evidence,
)
from .catalog import CatalogRepository, canonical_gop, normalize_gop
from .catalog_rule_validation import apply_catalog_rule_validation
from .evidence_extraction import quarter_from_date
from .models import BillingItem, Evidence, InvoiceSummary

def active_rules_payload() -> list[dict[str, str]]:
    return [
        {
            "rule_id": rule.rule_id,
            "evidence_kind": rule.evidence_kind,
            "gop_original": rule.gop,
            "title_hint": rule.title_hint,
            "valid_from": rule.valid_from or "",
            "valid_to": rule.valid_to or "",
            "regions": ",".join(rule.regions),
        }
        for rule in evidence_billing_rules()
    ]


def rule_overview_payload() -> dict[str, object]:
    guidance = billing_rule_guidance()
    return {
        "rule_set": guidance["rule_set"],
        "rules": active_rules_payload(),
        "candidate_rules": guidance["candidate_rules"],
        "temporal_rules": guidance["temporal_rules"],
        "event_sequence_rules": guidance["event_sequence_rules"],
        "derived_rules": guidance["derived_rules"],
    }


def generate_billing_items(
    evidence: list[Evidence],
    catalog: CatalogRepository,
    default_quarter: str | None,
    region: str = "Hessen",
) -> tuple[list[BillingItem], InvoiceSummary]:
    events = build_billing_events(evidence, default_quarter, region)
    billing_evidence = primary_episode_evidence(events)
    items: list[BillingItem] = []
    used_event_gops: set[tuple[str, str]] = set()

    for rule in evidence_billing_rules(default_quarter, region):
        for event in events_for_evidence_kind(events, rule.evidence_kind):
            selected = _select_best_evidence(event.evidence)
            fallback_gop = event.sequence_gop or rule.gop
            event_quarter = quarter_from_date(event.service_date) or default_quarter or "2025/Q4"
            rule_decision = resolve_evidence_rule_gop(
                rule.evidence_kind,
                fallback_gop,
                event.service_date,
                event.service_time,
                region,
                quarter=event_quarter,
            )
            gop_original = canonical_gop(rule_decision.gop or fallback_gop)
            gop_base, gop_suffix = normalize_gop(gop_original)
            event_gop_key = (gop_base, event.event_id)
            if event_gop_key in used_event_gops:
                continue
            used_event_gops.add(event_gop_key)

            entry = catalog.lookup(gop_original, event_quarter, region=region)
            validation_notes = list(rule_decision.notes)
            validation_status = "review" if rule_decision.review_required else "valid"
            sequence_suffix = f"+{event.sequence_rule_id}" if event.sequence_rule_id else ""
            rule_id = f"{rule.rule_id}{sequence_suffix}+{rule_decision.rule_id}"

            if not entry:
                validation_status = "catalog_missing"
                validation_notes.append(f"GOP {gop_base} wurde im Katalog {event_quarter} nicht gefunden.")
                title = rule.title_hint
                points = None
                amount = None
                source = "UNKNOWN"
                source_label = None
                catalog_id = None
                catalog_data_stand = None
            else:
                catalog_decision = evaluate_catalog_context_rules(
                    BillingRuleContext(
                        gop=gop_original,
                        service_date=event.service_date,
                        service_time=event.service_time,
                        region=region,
                        evidence_kind=rule.evidence_kind,
                        evidence_text=" ".join(item.text for item in event.evidence),
                        evidence_metadata=selected.metadata,
                        catalog_rule_texts=entry.rule_texts,
                    )
                )
                validation_notes.extend(catalog_decision.notes)
                if catalog_decision.review_required:
                    validation_status = "review"
                if catalog_decision.rule_id != "catalog.context.noop.v1":
                    rule_id = f"{rule_id}+{catalog_decision.rule_id}"
                title = entry.title
                points = entry.points
                amount = entry.euro
                source = entry.source
                source_label = entry.catalog_label
                catalog_id = entry.catalog_id
                catalog_data_stand = entry.data_stand

            items.append(
                BillingItem(
                    line=len(items) + 1,
                    gop_original=gop_original,
                    gop_base=gop_base,
                    gop_suffix=gop_suffix,
                    title=title,
                    catalog_source=source,
                    catalog_source_label=source_label,
                    catalog_id=catalog_id,
                    catalog_data_stand=catalog_data_stand,
                    quarter=event_quarter,
                    service_date=event.service_date,
                    service_time=event.service_time,
                    service_event_id=event.event_id,
                    service_session_id=event.session_id,
                    treatment_episode_id=event.episode_id,
                    temporal_role=event.temporal_role,
                    temporal_reason=event.temporal_reason,
                    quantity=1,
                    points=points,
                    amount_eur=amount,
                    rule_id=rule_id,
                    confidence="medium" if rule_decision.review_required else rule.confidence,
                    evidence_ids=event.evidence_ids,
                    evidence_pages=event.evidence_pages,
                    validation_status=validation_status,  # type: ignore[arg-type]
                    validation_notes=validation_notes,
                )
            )

    append_derived_billing_items(items, billing_evidence, catalog, default_quarter, region)
    _apply_catalog_rules_by_quarter(items, billing_evidence, catalog, region)
    finalize_billing_timeline(items)

    summary = InvoiceSummary(
        line_count=len(items),
        points_total=sum((item.points or 0) * item.quantity for item in items),
        amount_total_eur=round(sum((item.amount_eur or 0.0) * item.quantity for item in items), 2),
        human_review_required=True,
    )
    return items, summary


def _select_best_evidence(matches: list[Evidence]) -> Evidence:
    def score(item: Evidence) -> tuple[int, str, float]:
        has_date = 1 if item.service_date else 0
        service_datetime = f"{item.service_date or ''}T{item.service_time or '00:00'}"
        return has_date, service_datetime, item.confidence

    return sorted(matches, key=score, reverse=True)[0]


def append_derived_billing_items(
    items: list[BillingItem],
    evidence: list[Evidence],
    catalog: CatalogRepository,
    default_quarter: str | None,
    region: str,
) -> None:
    decision_contexts: list[tuple[DerivedGopDecision, list[BillingItem], list[Evidence]]] = []
    event_groups: dict[str, list[BillingItem]] = {}
    for item in items:
        group_key = item.service_event_id or f"date:{item.service_date or 'unknown'}"
        event_groups.setdefault(group_key, []).append(item)
    for group_items in event_groups.values():
        service_dates = {item.service_date for item in group_items if item.service_date}
        evidence_ids = {evidence_id for item in group_items for evidence_id in item.evidence_ids}
        related_evidence = [
            item
            for item in evidence
            if item.evidence_id in evidence_ids or not item.service_date or item.service_date in service_dates
        ]
        decisions = derive_additional_gops(
            [item.gop_original for item in group_items],
            [item.model_dump() for item in related_evidence],
            quarter=(group_items[0].quarter if group_items else None) or default_quarter,
            region=region,
        )
        decision_contexts.extend((decision, group_items, related_evidence) for decision in decisions)

    used_event_gops = {
        (item.gop_base, item.service_event_id or f"date:{item.service_date or 'unknown'}")
        for item in items
    }
    for decision, group_items, related_evidence in decision_contexts:
        if not decision.gop:
            continue
        gop_original = canonical_gop(decision.gop)
        gop_base, gop_suffix = normalize_gop(gop_original)
        anchor_base, _ = normalize_gop(decision.insert_after or "")
        base_item = next((item for item in group_items if item.gop_base == anchor_base), None)
        event_key = base_item.service_event_id if base_item else None
        dedupe_key = (gop_base, event_key or f"date:{base_item.service_date if base_item else 'unknown'}")
        if dedupe_key in used_event_gops:
            continue
        quarter = (
            (base_item.quarter if base_item else None)
            or default_quarter
            or _quarter_from_evidence(evidence)
            or "2025/Q4"
        )
        entry = catalog.lookup(gop_original, quarter, region=region)
        validation_notes = list(decision.notes)
        validation_status = "review" if decision.review_required else "valid"
        rule_id = decision.rule_id

        if not entry:
            validation_status = "catalog_missing"
            validation_notes.append(f"GOP {gop_base} wurde im Katalog {quarter} nicht gefunden.")
            title = decision.title_hint or gop_original
            points = None
            amount = None
            source = "UNKNOWN"
            source_label = None
            catalog_id = None
            catalog_data_stand = None
        else:
            catalog_decision = evaluate_catalog_context_rules(
                BillingRuleContext(
                    gop=gop_original,
                    service_date=base_item.service_date if base_item else None,
                    service_time=base_item.service_time if base_item else None,
                    region=region,
                    evidence_kind=decision.evidence_kind,
                    evidence_text=" ".join(item.text for item in related_evidence),
                    evidence_metadata=dict(decision.metadata or {}),
                    catalog_rule_texts=entry.rule_texts,
                )
            )
            validation_notes.extend(catalog_decision.notes)
            if catalog_decision.review_required:
                validation_status = "review"
            if catalog_decision.rule_id != "catalog.context.noop.v1":
                rule_id = f"{rule_id}+{catalog_decision.rule_id}"
            title = entry.title
            points = entry.points
            amount = entry.euro
            source = entry.source
            source_label = entry.catalog_label
            catalog_id = entry.catalog_id
            catalog_data_stand = entry.data_stand

        used_event_gops.add(dedupe_key)
        derived_item = BillingItem(
            line=0,
            gop_original=gop_original,
            gop_base=gop_base,
            gop_suffix=gop_suffix,
            title=title,
            catalog_source=source,
            catalog_source_label=source_label,
            catalog_id=catalog_id,
            catalog_data_stand=catalog_data_stand,
            quarter=quarter,
            service_date=base_item.service_date if base_item else None,
            service_time=base_item.service_time if base_item else None,
            service_event_id=event_key,
            service_session_id=base_item.service_session_id if base_item else None,
            treatment_episode_id=base_item.treatment_episode_id if base_item else None,
            temporal_role=base_item.temporal_role if base_item else "service_event",
            temporal_reason=base_item.temporal_reason if base_item else None,
            quantity=1,
            points=points,
            amount_eur=amount,
            rule_id=rule_id,
            confidence="medium" if validation_status == "review" else "high",
            evidence_ids=list(decision.evidence_ids),
            evidence_pages=list(decision.evidence_pages),
            validation_status=validation_status,  # type: ignore[arg-type]
            validation_notes=validation_notes,
            derivation_source="deterministic_rules",
            semantic_reason=decision.notes[0] if decision.notes else None,
        )
        insert_index = items.index(base_item) + 1 if base_item in items else len(items)
        while insert_index < len(items) and items[insert_index].rule_id.startswith("derived."):
            insert_index += 1
        items.insert(insert_index, derived_item)

    for index, item in enumerate(items, start=1):
        item.line = index


def reconcile_derived_item_anchors(
    items: list[BillingItem],
    quarter: str | None,
    region: str,
    rule_set: BillingRuleSet | None = None,
) -> None:
    definitions = rule_set or get_runtime_billing_rule_set(quarter, region)
    rules_by_target: dict[str, list[DerivedRuleDefinition]] = {}
    for rule in definitions.derived_rules:
        if not rule.insert_after or not definition_is_applicable(
            rule.valid_from,
            rule.valid_to,
            rule.regions,
            quarter,
            region,
        ):
            continue
        target_base, _ = normalize_gop(rule.gop)
        rules_by_target.setdefault(target_base, []).append(rule)

    anchored: list[tuple[BillingItem, BillingItem, bool]] = []
    for item in list(items):
        applicable_rules = [
            rule
            for rule in rules_by_target.get(item.gop_base, [])
            if definition_is_applicable(
                rule.valid_from,
                rule.valid_to,
                rule.regions,
                item.quarter or quarter,
                region,
            )
        ]
        rule = applicable_rules[0] if applicable_rules else None
        if not rule or not rule.insert_after:
            continue
        anchor_base, _ = normalize_gop(rule.insert_after)
        anchors = [candidate for candidate in items if candidate is not item and candidate.gop_base == anchor_base]
        anchor = _select_derived_anchor(item, anchors)
        if not anchor:
            continue
        already_anchored = bool(item.service_event_id and item.service_event_id == anchor.service_event_id)
        _copy_temporal_anchor(item, anchor)
        anchored.append((item, anchor, already_anchored))

    quality = {
        id(item): (
            int(already_anchored),
            int(item.derivation_source == "deterministic_rules"),
            int(item.validation_status == "valid"),
            int(item.confidence == "high"),
        )
        for item, _anchor, already_anchored in anchored
    }
    by_event_gop: dict[tuple[str, str], list[BillingItem]] = {}
    for item, _anchor, _already_anchored in anchored:
        event_key = item.service_event_id or f"date:{item.service_date or 'unknown'}"
        by_event_gop.setdefault((item.gop_base, event_key), []).append(item)
    for duplicates in by_event_gop.values():
        if len(duplicates) < 2:
            continue
        keep = max(duplicates, key=lambda candidate: quality[id(candidate)])
        duplicate_ids = {id(candidate) for candidate in duplicates}
        items[:] = [candidate for candidate in items if candidate is keep or id(candidate) not in duplicate_ids]
        anchored = [entry for entry in anchored if entry[0] is keep or id(entry[0]) not in duplicate_ids]

    derived_ids = {id(item) for item, _anchor, _already_anchored in anchored}
    for item, anchor, _already_anchored in anchored:
        if not any(candidate is item for candidate in items) or not any(candidate is anchor for candidate in items):
            continue
        item_index = next(index for index, candidate in enumerate(items) if candidate is item)
        items.pop(item_index)
        insert_index = next(index for index, candidate in enumerate(items) if candidate is anchor) + 1
        while insert_index < len(items) and id(items[insert_index]) in derived_ids:
            insert_index += 1
        items.insert(insert_index, item)
    for index, item in enumerate(items, start=1):
        item.line = index


def _select_derived_anchor(item: BillingItem, anchors: list[BillingItem]) -> BillingItem | None:
    if not anchors:
        return None
    return min(
        anchors,
        key=lambda anchor: (
            0 if item.service_event_id and item.service_event_id == anchor.service_event_id else 1,
            0 if item.treatment_episode_id and item.treatment_episode_id == anchor.treatment_episode_id else 1,
            0 if item.service_date and item.service_date == anchor.service_date else 1,
            _temporal_distance_minutes(item, anchor),
            anchor.line,
        ),
    )


def _temporal_distance_minutes(item: BillingItem, anchor: BillingItem) -> float:
    if not item.service_date or not anchor.service_date:
        return float("inf")
    try:
        item_value = datetime.fromisoformat(f"{item.service_date}T{item.service_time or '00:00'}")
        anchor_value = datetime.fromisoformat(f"{anchor.service_date}T{anchor.service_time or '00:00'}")
    except ValueError:
        return float("inf")
    return abs((item_value - anchor_value).total_seconds()) / 60


def _copy_temporal_anchor(item: BillingItem, anchor: BillingItem) -> None:
    item.service_date = anchor.service_date
    item.service_time = anchor.service_time
    item.service_event_id = anchor.service_event_id
    item.service_session_id = anchor.service_session_id
    item.treatment_episode_id = anchor.treatment_episode_id
    item.temporal_role = anchor.temporal_role
    item.temporal_reason = anchor.temporal_reason


def _quarter_from_evidence(evidence: list[Evidence]) -> str | None:
    dates = sorted(item.service_date for item in evidence if item.service_date)
    return quarter_from_date(dates[0]) if dates else None


def _apply_catalog_rules_by_quarter(
    items: list[BillingItem],
    evidence: list[Evidence],
    catalog: CatalogRepository,
    region: str,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    for quarter in sorted({item.quarter for item in items}):
        quarter_items = [item for item in items if item.quarter == quarter]
        results.append(apply_catalog_rule_validation(quarter_items, evidence, catalog, quarter, region))
    return results
