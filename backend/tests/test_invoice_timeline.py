from app.billing_events import build_billing_events
from app.invoice_timeline import build_invoice_timeline
from app.models import BillingItem, Evidence


def _evidence(
    evidence_id: str,
    kind: str,
    label: str,
    service_time: str,
    metadata: dict[str, object],
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        kind=kind,
        label=label,
        page=1,
        service_date="2026-04-24",
        service_time=service_time,
        text=label,
        metadata=metadata,
    )


def test_invoice_timeline_keeps_admission_without_gop_and_links_physician_contact():
    evidence = [
        _evidence(
            "ev-admission",
            "timeline.administrative_admission",
            "Aufnahme",
            "18:50",
            {
                "timeline_event_type": "administrative_admission",
                "timeline_label": "Aufnahme",
                "timeline_reason": "Administrativer Aufnahmezeitpunkt.",
            },
        ),
        _evidence(
            "ev-contact",
            "context.kv_notfall_zna",
            "Erster persönlicher Arztkontakt",
            "19:05",
            {
                "timeline_event_type": "first_personal_physician_contact",
                "timeline_label": "Erster persönlicher Arztkontakt",
            },
        ),
    ]
    events = build_billing_events(evidence, "2026/Q2", "Hessen")
    contact_event = next(event for event in events if event.kind == "context.kv_notfall_zna")
    item = BillingItem(
        line=1,
        gop_original="01212",
        gop_base="01212",
        title="Notfallpauschale II",
        catalog_source="EBM_KBV",
        quarter="2026/Q2",
        service_date=contact_event.service_date,
        service_time=contact_event.service_time,
        service_event_id=contact_event.event_id,
        evidence_ids=contact_event.evidence_ids,
        evidence_pages=contact_event.evidence_pages,
        rule_id="test.rule",
        confidence="high",
    )

    timeline = build_invoice_timeline(events, [item], "2026/Q2", "Hessen")

    assert [(event.label, event.service_time, event.gops) for event in timeline] == [
        ("Aufnahme", "18:50", []),
        ("Erster persönlicher Arztkontakt", "19:05", ["01212"]),
    ]
    assert item.temporal_sequence == 2


def test_configured_contacts_are_not_merged_into_one_event():
    evidence = [
        _evidence(
            "ev-first-contact",
            "context.kv_notfall_zna",
            "Erster persönlicher Arztkontakt",
            "19:05",
            {"force_separate_event": True},
        ),
        _evidence(
            "ev-follow-up-contact",
            "context.kv_notfall_zna",
            "Weiterer persönlicher Arztkontakt",
            "19:30",
            {"force_separate_event": True},
        ),
    ]

    events = build_billing_events(evidence, "2026/Q2", "Hessen")

    assert len(events) == 2
    assert [event.temporal_role for event in events] == ["initial_contact", "follow_up_contact"]


def test_timeline_shows_the_course_of_treatment_without_any_billing_item():
    """Scheitert die Ableitung, ist die Zeitleiste die einzige verbliebene Auskunft."""
    evidence = [
        Evidence(
            evidence_id="ev-1",
            kind="clinical.diagnostics.ctg",
            label="CTG / Tokographie",
            page=1,
            service_date="2026-01-01",
            service_time="13:05",
            text="CTG",
        ),
        Evidence(
            evidence_id="ev-2",
            kind="clinical.diagnostics.sonography",
            label="Sonographie",
            page=2,
            service_date="2026-01-01",
            service_time="13:15",
            text="Sonographie",
        ),
    ]
    events = build_billing_events(evidence, "2026/Q1", "Hessen")

    timeline = build_invoice_timeline(events, [], "2026/Q1", "Hessen")

    assert [entry.service_time for entry in timeline] == ["13:05", "13:15"]
    assert all(entry.gops == [] for entry in timeline)
