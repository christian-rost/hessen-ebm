from pathlib import Path

from app.billing_events import build_billing_events
from app.billing_rule_definitions import load_billing_rule_set, parse_billing_rule_set
from app.billing_rule_store import build_rule_set_row
from app.clinical_definitions import load_clinical_definition_set
from app.clinical_definitions import parse_clinical_definition_set
from app.document_segmentation import segment_pages
from app.evidence_extraction import extract_evidence
from app.ebm_rule_compiler import CompiledCatalogRuleSet
from app.models import Evidence, PageText


EXECUTION_MODULES = (
    "clinical_rule_engine.py",
    "document_segmentation.py",
    "evidence_extraction.py",
)
FORBIDDEN_DOMAIN_LITERALS = (
    "all_ord",
    "aua_",
    "ctg",
    "imeron",
    "notfall",
    "pco2",
    "po2",
    "racth",
    "sonograf",
    "sonograph",
)


def test_execution_modules_contain_no_clinical_or_internal_code_hardcoding() -> None:
    app_dir = Path(__file__).resolve().parents[1] / "app"
    violations: list[str] = []
    for filename in EXECUTION_MODULES:
        source = (app_dir / filename).read_text(encoding="utf-8").casefold()
        for literal in FORBIDDEN_DOMAIN_LITERALS:
            if literal in source:
                violations.append(f"{filename}: {literal}")

    assert violations == [], (
        "Fachbegriffe und interne Leistungscodes gehören in clinical_evidence_definitions.json, "
        "nicht in den ausführenden Python-Code:\n" + "\n".join(violations)
    )


def test_new_segment_and_evidence_can_be_added_without_python_change() -> None:
    definitions = parse_clinical_definition_set(
        {
            "schema_version": 1,
            "definition_set_id": "configuration-extension-test",
            "version": "1",
            "formats": {
                "fallback_segment_type": "other",
                "date_regex": r"(\d{2}\.\d{2}\.\d{4})",
                "time_regex": r"(\d{2}:\d{2})",
            },
            "segment_types": {
                "configured_document": {
                    "label": "Konfigurierter Dokumenttyp",
                    "flags": ["billing_relevant", "clinical_context"],
                },
                "other": {"label": "Sonstiges Dokument", "flags": []},
            },
            "segment_classifiers": [
                {
                    "rule_id": "segment.configured.v1",
                    "segment_type": "configured_document",
                    "confidence": 0.99,
                    "reason": "Konfigurationsmarker erkannt",
                    "when": {"text_any": ["konfigurationsmarker"]},
                }
            ],
            "datetime_roles": {},
            "state_tracks": [],
            "context_updates": [],
            "evidence_rules": [
                {
                    "rule_id": "evidence.configured.v1",
                    "kind": "configured.evidence",
                    "label": "Konfigurierte Evidenz",
                    "confidence": 0.91,
                    "when": {
                        "all": [
                            {"segment_any": ["configured_document"]},
                            {"text_any": ["neueleistung"]},
                        ]
                    },
                }
            ],
            "review_rules": [],
            "exclusion_rules": [],
        }
    )
    pages = [PageText(page=1, text="Konfigurationsmarker NeueLeistung")]

    segments = segment_pages(pages, definitions)
    evidence, review, excluded, _context = extract_evidence(pages, segments, definitions)

    assert segments[0].segment_type == "configured_document"
    assert segments[0].relevant_for_billing is True
    assert [item.kind for item in evidence] == ["configured.evidence"]
    assert review == []
    assert excluded == []


def test_event_clustering_uses_rule_set_settings_instead_of_code_constants() -> None:
    rule_set = parse_billing_rule_set(
        {
            "schema_version": 1,
            "rule_set_id": "event-settings-test",
            "version": "1",
            "event_settings": {
                "default_session_gap_minutes": 10,
                "episode_gap_days": 3,
                "timeline_role_priority": {"service_event": 1},
                "anchor_strategies": [
                    {"sort_fields": ["service_date", "service_time", "page"]}
                ],
            },
            "calendar_definitions": {},
            "evidence_rules": [],
            "event_sequence_rules": [],
            "temporal_rules": [],
            "derived_rules": [],
        }
    )
    evidence = [
        Evidence(
            evidence_id="configured-event-1",
            kind="configured.event",
            label="Ereignis",
            page=1,
            service_date="2026-01-01",
            service_time="10:00",
            text="Erstes Ereignis",
        ),
        Evidence(
            evidence_id="configured-event-2",
            kind="configured.event",
            label="Ereignis",
            page=2,
            service_date="2026-01-01",
            service_time="10:30",
            text="Zweites Ereignis",
        ),
    ]

    events = build_billing_events(evidence, "2026/Q1", "Testregion", rule_set)

    assert len(events) == 2


def test_supabase_rule_payload_contains_versioned_clinical_definitions() -> None:
    compiled = CompiledCatalogRuleSet(
        rule_set_id="compiled-test",
        version="1",
        quarter="2026/Q1",
        region="Hessen",
        source_catalog_id="catalog-test",
        source_data_stand="2026-01-01",
        source_hash="hash",
        compiled_at="2026-01-01T00:00:00+00:00",
        rules=(),
    )
    clinical = load_clinical_definition_set()

    row = build_rule_set_row(compiled, load_billing_rule_set(), clinical)

    assert row["core_payload"]["clinical_definitions"]["definition_set_id"] == clinical.definition_set_id
    assert row["core_payload"]["clinical_definitions"]["version"] == clinical.version
