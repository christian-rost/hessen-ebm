from __future__ import annotations

from collections.abc import Iterable

from .billing_events import BillingEvent
from .billing_rule_definitions import BillingRuleSet
from .billing_rule_store import get_runtime_billing_rule_set
from .models import BillingItem, Evidence, InvoiceTimelineEvent


def build_invoice_timeline(
    events: list[BillingEvent],
    items: list[BillingItem],
    quarter: str | None,
    region: str,
    rule_set: BillingRuleSet | None = None,
) -> list[InvoiceTimelineEvent]:
    definitions = rule_set or get_runtime_billing_rule_set(quarter, region)
    type_priority = {
        str(event_type): int(priority)
        for event_type, priority in (definitions.event_settings.get("timeline_event_type_priority") or {}).items()
    }
    items_by_event: dict[str, list[BillingItem]] = {}
    for item in items:
        if item.service_event_id:
            items_by_event.setdefault(item.service_event_id, []).append(item)

    timeline: list[InvoiceTimelineEvent] = []
    matched_item_ids: set[int] = set()
    for event in events:
        event_items = items_by_event.get(event.event_id, [])
        metadata = _timeline_metadata(event.evidence)
        if not metadata and not event_items:
            continue
        matched_item_ids.update(id(item) for item in event_items)
        event_type = str(metadata.get("timeline_event_type") or "service_event")
        timeline.append(
            InvoiceTimelineEvent(
                event_id=event.event_id,
                sequence=0,
                event_type=event_type,
                label=str(metadata.get("timeline_label") or _item_label(event_items) or event.evidence[0].label),
                service_date=event.service_date,
                service_time=event.service_time,
                temporal_role=_timeline_role(event_type, event.temporal_role),
                reason=str(metadata.get("timeline_reason") or event.temporal_reason or "") or None,
                gops=_unique(item.gop_original for item in event_items),
                billing_item_lines=[item.line for item in event_items],
                evidence_ids=event.evidence_ids,
                evidence_pages=event.evidence_pages,
            )
        )

    for item in items:
        if id(item) in matched_item_ids:
            continue
        timeline.append(
            InvoiceTimelineEvent(
                event_id=item.service_event_id or f"billing-item-{item.line}",
                sequence=0,
                event_type="service_event",
                label=item.title,
                service_date=item.service_date,
                service_time=item.service_time,
                temporal_role=item.temporal_role,
                reason=item.temporal_reason,
                gops=[item.gop_original],
                billing_item_lines=[item.line],
                evidence_ids=item.evidence_ids,
                evidence_pages=item.evidence_pages,
            )
        )

    timeline.sort(
        key=lambda event: (
            event.service_date or "9999-12-31",
            event.service_time or "23:59",
            type_priority.get(event.event_type, type_priority.get("service_event", 100)),
            min(event.evidence_pages) if event.evidence_pages else 0,
            event.event_id,
        )
    )
    sequence_by_event_id: dict[str, int] = {}
    for sequence, event in enumerate(timeline, start=1):
        event.sequence = sequence
        sequence_by_event_id[event.event_id] = sequence

    for item in items:
        event_id = item.service_event_id or f"billing-item-{item.line}"
        item.temporal_sequence = sequence_by_event_id.get(event_id)
    return timeline


def _timeline_metadata(evidence: list[Evidence]) -> dict[str, object]:
    for item in evidence:
        if item.metadata.get("timeline_event_type"):
            return item.metadata
    return {}


def _timeline_role(event_type: str, fallback: str) -> str:
    return event_type if event_type != "service_event" else fallback


def _item_label(items: list[BillingItem]) -> str | None:
    titles = _unique(item.title for item in items)
    if len(titles) == 1:
        return titles[0]
    if titles:
        return "Abrechenbare Leistungen"
    return None


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))
