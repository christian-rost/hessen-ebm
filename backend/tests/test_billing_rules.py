import pytest

from app.billing_rules import (
    BillingRuleContext,
    apply_temporal_gop_rule,
    candidate_gops_for_evidence_kind,
    derive_additional_gops,
    evaluate_catalog_context_rules,
    evaluate_gop_rules,
    is_special_notfall_day,
)
from app.billing_rule_definitions import parse_billing_rule_set
from app.billing_rule_store import _merge_runtime_core_rules


def test_emergency_initial_gop_uses_weekday_daytime_01210():
    decision = apply_temporal_gop_rule("01210", "2026-04-24", "12:20")

    assert decision.gop == "01210"
    assert decision.review_required is False


def test_emergency_initial_gop_uses_01212_at_night_weekend_and_special_days():
    assert apply_temporal_gop_rule("01210", "2026-04-24", "20:00").gop == "01212"
    assert apply_temporal_gop_rule("01210", "2026-04-25", "12:00").gop == "01212"
    assert apply_temporal_gop_rule("01210", "2026-12-24", "12:00").gop == "01212"
    assert apply_temporal_gop_rule("01210", "2026-06-04", "12:00").gop == "01212"


def test_missing_datetime_requires_review_for_time_dependent_emergency_gops():
    decision = apply_temporal_gop_rule("01210", "2026-04-24", None)

    assert decision.gop is None
    assert decision.review_required is True
    assert "Datum oder Uhrzeit fehlt" in decision.notes[0]


def test_emergency_consultation_gop_uses_time_windows():
    assert apply_temporal_gop_rule("01214", "2026-04-24", "12:20").gop == "01214"
    assert apply_temporal_gop_rule("01214", "2026-04-24", "20:00").gop == "01216"
    assert apply_temporal_gop_rule("01214", "2026-04-24", "23:00").gop == "01218"
    assert apply_temporal_gop_rule("01214", "2026-04-25", "12:00").gop == "01216"
    assert apply_temporal_gop_rule("01214", "2026-04-25", "20:00").gop == "01218"


def test_emergency_clarification_gop_uses_daytime_pair():
    assert apply_temporal_gop_rule("01205", "2026-04-24", "12:20").gop == "01205"
    assert apply_temporal_gop_rule("01205", "2026-04-24", "20:00").gop == "01207"
    assert apply_temporal_gop_rule("01205", "2026-04-25", "12:00").gop == "01207"


def test_apply_temporal_gop_rule_corrects_wrong_emergency_pair():
    decision = apply_temporal_gop_rule("01210", "2026-04-24", "20:00")

    assert decision.gop == "01212"
    assert "korrigiert" in decision.notes[0]


def test_apply_temporal_gop_rule_normalizes_missing_leading_zero():
    decision = apply_temporal_gop_rule("1212", "2026-04-24", "20:00")

    assert decision.gop == "01212"


def test_derive_01226_for_01212_with_cognitive_or_dementia_criteria():
    decisions = derive_additional_gops(
        ["01212"],
        [
            {
                "evidence_id": "ev-notfall",
                "kind": "context.kv_notfall_zna",
                "label": "KV-Notfall/ZNA",
                "page": 5,
                "text": "Persönlicher Arzt-Patienten-Kontakt in der ZNA",
            },
            {
                "evidence_id": "ev-f03",
                "kind": "diagnosis.icd10",
                "label": "ICD-10 F03",
                "page": 28,
                "value": "F03",
                "text": "Nicht näher bezeichnete Demenz (F03), gesichert",
                "metadata": {"icd10": "F03"},
            },
            {
                "evidence_id": "ev-report",
                "kind": "clinical.domain.neurology",
                "label": "Neurologie",
                "page": 11,
                "text": "Alter 81 J. schwere Demenz, zu allen Qualitäten desorientiert",
            },
        ],
    )
    decision = decisions[0]

    assert decision.gop == "01226"
    assert "ev-f03" in decision.evidence_ids
    assert decision.metadata
    assert decision.metadata["patient_age"] == 81


def test_derive_01226_does_not_apply_to_01210():
    decisions = derive_additional_gops(
        ["01210"],
        [
            {
                "evidence_id": "ev-f03",
                "kind": "diagnosis.icd10",
                "label": "ICD-10 F03",
                "page": 1,
                "value": "F03",
                "text": "Nicht näher bezeichnete Demenz (F03)",
                "metadata": {"icd10": "F03"},
            }
        ],
    )

    assert decisions == []


def test_data_driven_rules_support_new_temporal_and_chained_gops_without_python_changes():
    rule_set = parse_billing_rule_set(
        {
            "schema_version": 1,
            "rule_set_id": "generic-test-rules",
            "version": "1.0",
            "evidence_rules": [
                {
                    "rule_id": "test.service.base.v1",
                    "evidence_kind": "test.service",
                    "gop": "11111",
                    "title_hint": "Testleistung",
                    "valid_from": "2026/Q1",
                    "regions": ["*"],
                }
            ],
            "candidate_rules": [
                {
                    "rule_id": "test.candidate.v1",
                    "evidence_kind": "test.candidate-only",
                    "gops": ["22221", "22222"],
                    "valid_from": "2026/Q1",
                    "regions": ["*"],
                }
            ],
            "temporal_rules": [
                {
                    "rule_id": "test.temporal.v1",
                    "name": "Generische Zeitvariante",
                    "gops": ["11111", "11112"],
                    "required_context": ["service_date", "service_time"],
                    "valid_from": "2026/Q1",
                    "regions": ["*"],
                    "outcomes": [
                        {
                            "rule_id": "test.temporal.night.v1",
                            "gop": "11112",
                            "when": {"time_window": {"start": "07:00", "end": "19:00", "inside": False}},
                            "note": "Nächtliche Testvariante.",
                        },
                        {
                            "rule_id": "test.temporal.day.v1",
                            "gop": "11111",
                            "when": {"time_window": {"start": "07:00", "end": "19:00", "inside": True}},
                            "note": "Tägliche Testvariante.",
                        },
                    ],
                }
            ],
            "derived_rules": [
                {
                    "rule_id": "test.derived.first.v1",
                    "gop": "09999",
                    "title_hint": "Erster generischer Zuschlag",
                    "evidence_kind": "derived.test.first",
                    "insert_after": "11112",
                    "valid_from": "2026/Q1",
                    "regions": ["*"],
                    "requirements": [{"gop_present": "11112"}],
                    "criteria": [
                        {"label": "passende Evidenz", "when": {"evidence_kind": "test.qualifier"}}
                    ],
                },
                {
                    "rule_id": "test.derived.second.v1",
                    "gop": "09998",
                    "title_hint": "Verketteter generischer Zuschlag",
                    "evidence_kind": "derived.test.second",
                    "insert_after": "09999",
                    "valid_from": "2026/Q1",
                    "regions": ["*"],
                    "requirements": [{"gop_present": "09999"}],
                },
            ],
        }
    )

    candidates = candidate_gops_for_evidence_kind(
        "test.service", "2026/Q2", "Hessen", rule_set
    )
    configured_candidates = candidate_gops_for_evidence_kind(
        "test.candidate-only", "2026/Q2", "Hessen", rule_set
    )
    temporal = apply_temporal_gop_rule(
        "11111", "2026-04-24", "23:00", "Hessen", quarter="2026/Q2", rule_set=rule_set
    )
    decisions = derive_additional_gops(
        [temporal.gop or ""],
        [{"kind": "test.qualifier", "text": "Qualifizierende Evidenz", "page": 1}],
        quarter="2026/Q2",
        rule_set=rule_set,
    )

    assert candidates == ["11111", "11112"]
    assert configured_candidates == ["22221", "22222"]
    assert temporal.gop == "11112"
    assert [decision.gop for decision in decisions] == ["09999", "09998"]


def test_rule_definitions_reject_unknown_condition_operators_during_loading():
    with pytest.raises(ValueError, match="Unbekannter Regeloperator"):
        parse_billing_rule_set(
            {
                "schema_version": 1,
                "rule_set_id": "invalid-test-rules",
                "version": "1.0",
                "evidence_rules": [],
                "temporal_rules": [],
                "derived_rules": [
                    {
                        "rule_id": "test.invalid.v1",
                        "gop": "09999",
                        "title_hint": "Ungültige Regel",
                        "evidence_kind": "derived.invalid",
                        "requirements": [{"unbekannt": True}],
                    }
                ],
            }
        )


def test_derived_rule_validity_is_filtered_by_quarter_and_region():
    rule_set = parse_billing_rule_set(
        {
            "schema_version": 1,
            "rule_set_id": "validity-test-rules",
            "version": "1.0",
            "evidence_rules": [],
            "temporal_rules": [],
            "derived_rules": [
                {
                    "rule_id": "test.validity.v1",
                    "gop": "09999",
                    "title_hint": "Regional und zeitlich begrenzte Regel",
                    "evidence_kind": "derived.validity",
                    "valid_from": "2026/Q2",
                    "valid_to": "2026/Q3",
                    "regions": ["Hessen"],
                    "requirements": [{"gop_present": "11111"}],
                }
            ],
        }
    )

    assert derive_additional_gops(["11111"], [], "2026/Q1", "Hessen", rule_set) == []
    assert derive_additional_gops(["11111"], [], "2026/Q2", "Bayern", rule_set) == []
    assert [
        decision.gop
        for decision in derive_additional_gops(["11111"], [], "2026/Q2", "Hessen", rule_set)
    ] == ["09999"]


def test_special_notfall_day_includes_hessen_public_holiday():
    assert is_special_notfall_day("2026-06-04", "Hessen") is True


def test_special_day_calendar_is_supplied_by_the_rule_set() -> None:
    rule_set = parse_billing_rule_set(
        {
            "schema_version": 1,
            "rule_set_id": "calendar-test",
            "version": "1",
            "event_settings": {},
            "calendar_definitions": {
                "default": {
                    "weekdays": [],
                    "fixed_dates": ["02-03"],
                    "easter_offsets": [],
                },
                "regions": {},
            },
            "evidence_rules": [],
            "temporal_rules": [],
            "derived_rules": [],
        }
    )

    assert is_special_notfall_day("2026-02-03", "Testregion", rule_set) is True
    assert is_special_notfall_day("2026-02-04", "Testregion", rule_set) is False


def test_catalog_context_rules_require_review_for_missing_structured_context():
    decision = evaluate_catalog_context_rules(
        BillingRuleContext(
            gop="99999",
            service_date="2026-04-24",
            service_time=None,
            catalog_rule_texts=(
                "Die Uhrzeit der Inanspruchnahme ist anzugeben. "
                "Berechnungsfähig nur bis zum vollendeten 18. Lebensjahr und bei gesicherter Diagnose.",
            ),
        )
    )

    assert decision.review_required is True
    assert decision.gop is None
    assert any("Uhrzeit" in note for note in decision.notes)
    assert any("Altersbedingung" in note for note in decision.notes)
    assert any("Diagnose" in note for note in decision.notes)


def test_generic_gop_rule_evaluation_combines_correction_and_catalog_review():
    decision = evaluate_gop_rules(
        BillingRuleContext(
            gop="01210",
            service_date="2026-04-24",
            service_time="20:00",
            catalog_rule_texts=("Die GOP ist höchstens einmal im Behandlungsfall berechnungsfähig.",),
        )
    )

    assert decision.gop == "01212"
    assert decision.review_required is True
    assert "time.notfall.initial.01212.v1" in decision.rule_id
    assert "catalog.context.review.v1" in decision.rule_id
    assert any("Häufigkeitsbegrenzung" in note for note in decision.notes)


def test_runtime_rule_overlay_keeps_new_local_event_rules_until_supabase_is_recompiled():
    remote = parse_billing_rule_set(
        {
            "schema_version": 1,
            "rule_set_id": "remote",
            "version": "2026.1",
            "evidence_rules": [],
            "temporal_rules": [],
            "derived_rules": [],
        }
    )
    local = parse_billing_rule_set(
        {
            "schema_version": 1,
            "rule_set_id": "local",
            "version": "2026.2",
            "evidence_rules": [],
            "candidate_rules": [
                {
                    "rule_id": "candidate.test.v1",
                    "evidence_kind": "test.hint",
                    "gops": ["22221"],
                    "regions": ["*"],
                }
            ],
            "event_sequence_rules": [
                {
                    "rule_id": "sequence.test.v1",
                    "name": "Testsequenz",
                    "evidence_kinds": ["test.contact"],
                    "initial_gop": "11111",
                    "subsequent_gop": "11112",
                    "regions": ["*"],
                }
            ],
            "temporal_rules": [],
            "derived_rules": [],
        }
    )

    merged = _merge_runtime_core_rules(local, remote)

    assert [rule.rule_id for rule in merged.event_sequence_rules] == ["sequence.test.v1"]
    assert [rule.rule_id for rule in merged.candidate_rules] == ["candidate.test.v1"]
    assert merged.version == "2026.1+core-2026.2"
