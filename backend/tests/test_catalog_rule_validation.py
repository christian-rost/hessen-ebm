from collections import Counter

from app.catalog_rule_validation import ADVISORY, VIOLATION, _counts_for_scope, _evaluate_clause, _evidence_facts
from app.billing_rule_definitions import load_billing_rule_set
from app.clinical_definitions import (
    ClinicalDefinitionSet,
    clinical_definition_set_payload,
    load_clinical_definition_set,
    parse_clinical_definition_set,
)
from app.models import BillingItem, Evidence
from app.semantic_billing import _split_items_by_catalog_verdict


def item(gop: str, day: str, session: str, line: int) -> BillingItem:
    return BillingItem(
        line=line,
        gop_original=gop,
        gop_base=gop[:5],
        title=gop,
        catalog_source="EBM_KBV",
        quarter="2026/Q1",
        service_date=day,
        service_session_id=session,
        rule_id="test",
        confidence="high",
        evidence_ids=[],
        evidence_pages=[],
    )


def test_catalog_counts_follow_session_day_and_case_scope() -> None:
    first = item("01786", "2026-01-01", "session-1", 1)
    same_day = item("33042", "2026-01-01", "session-1", 2)
    later = item("01786", "2026-01-03", "session-2", 3)
    items = [first, same_day, later]

    assert _counts_for_scope(items, first, "same_session") == {"01786": 1, "33042": 1}
    assert _counts_for_scope(items, first, "treatment_day") == {"01786": 1, "33042": 1}
    assert _counts_for_scope(items, first, "treatment_case") == {"01786": 2, "33042": 1}


def test_longitudinal_frequency_requires_patient_history_check() -> None:
    current = item("99999", "2026-01-03", "session-1", 1)
    clause = {
        "clause_type": "frequency_limit",
        "scope": "disease_case",
        "parameters": {"maximum": 1},
        "source_text": "einmal im Krankheitsfall",
    }

    verdict = _evaluate_clause(clause, current, Counter({"99999": 1}), {})

    assert verdict is not None
    # Faelle ausserhalb des Dokuments sind unentscheidbar, also kein Abrechnungsstopp.
    assert verdict.severity == ADVISORY
    assert "patientenbezogene Abrechnungshistorie" in verdict.note


def evidence(kind: str, text: str = "", metadata: dict | None = None) -> Evidence:
    return Evidence(
        evidence_id=f"ev-{kind}",
        kind=kind,
        label=kind,
        page=1,
        text=text,
        metadata=metadata or {},
    )


def definitions_with_clause_fact(fact: dict) -> ClinicalDefinitionSet:
    payload = clinical_definition_set_payload(load_clinical_definition_set())
    payload["clause_facts"] = fact
    return parse_clinical_definition_set(payload)


def test_personal_contact_fact_comes_from_evidence_metadata_not_python() -> None:
    definitions = load_clinical_definition_set()
    flagged = [
        rule["kind"]
        for rule in definitions.evidence_rules
        if (rule.get("metadata") or {}).get("personal_contact")
    ]
    assert flagged, "Kein Evidenzrule traegt das Flag personal_contact"

    facts = _evidence_facts([evidence(flagged[0], metadata={"personal_contact": True})], definitions)
    assert facts["flags"]["personal_contact"] is True

    missing = _evidence_facts([evidence("lab.result.sodium")], definitions)
    assert missing["flags"]["personal_contact"] is False


def test_requires_clause_is_resolved_against_configured_clause_facts() -> None:
    definitions = load_clinical_definition_set()
    clause = {"clause_type": "requires_personal_contact", "source_text": "persoenlicher Arzt-Patienten-Kontakt"}
    current = item("99999", "2026-01-03", "session-1", 1)

    satisfied = _evidence_facts([evidence("lab.result.sodium", text="Arzt-Patienten-Kontakt dokumentiert")], definitions)
    assert _evaluate_clause(clause, current, Counter(), satisfied) is None

    unsatisfied = _evidence_facts([evidence("lab.result.sodium")], definitions)
    verdict = _evaluate_clause(clause, current, Counter(), unsatisfied)
    assert verdict is not None
    assert verdict.severity == VIOLATION
    assert "Arzt-Patienten-Kontakt" in verdict.note


def test_new_requires_clause_needs_no_python_change() -> None:
    definitions = definitions_with_clause_fact(
        {
            "written_report": {
                "label": "schriftlicher Befundbericht",
                "missing_note": "Katalogregel verlangt einen schriftlichen Befundbericht.",
                "evidence_kinds": ["configured.report"],
            }
        }
    )
    clause = {"clause_type": "requires_written_report", "source_text": "schriftlicher Bericht"}
    current = item("99999", "2026-01-03", "session-1", 1)

    satisfied = _evidence_facts([evidence("configured.report")], definitions)
    assert _evaluate_clause(clause, current, Counter(), satisfied) is None

    unsatisfied = _evidence_facts([evidence("configured.other")], definitions)
    verdict = _evaluate_clause(clause, current, Counter(), unsatisfied)
    assert verdict is not None
    assert verdict.severity == VIOLATION
    assert verdict.note == "Katalogregel verlangt einen schriftlichen Befundbericht."



def test_unknown_requires_clause_stays_silent() -> None:
    definitions = definitions_with_clause_fact({})
    clause = {"clause_type": "requires_something_undefined", "source_text": "unbekannt"}
    current = item("99999", "2026-01-03", "session-1", 1)

    facts = _evidence_facts([evidence("configured.other")], definitions)
    assert _evaluate_clause(clause, current, Counter(), facts) is None


def test_catalog_verdict_blocks_only_decidable_violations() -> None:
    """Das Abrechnungstor: Verletzungen stoppen, Unentscheidbares nur vermerken."""
    billable = item("34241", "2026-01-03", "session-1", 1)
    blocked = item("01210", "2026-01-03", "session-1", 2)
    validation = [
        {
            "item_verdicts": [
                {
                    "gop_original": "34241",
                    "service_event_id": billable.service_event_id,
                    "violations": [],
                    "advisories": ["Manuelle Prüfung der Katalogbedingung erforderlich: Videosprechstunde"],
                    "billable": True,
                },
                {
                    "gop_original": "01210",
                    "service_event_id": blocked.service_event_id,
                    "violations": ["Abrechnungsausschluss (in derselben Sitzung): nicht zusammen mit 01214."],
                    "advisories": [],
                    "billable": False,
                },
            ]
        }
    ]

    kept, review, hints = _split_items_by_catalog_verdict([billable, blocked], validation)

    assert [entry.gop_original for entry in kept] == ["34241"]
    assert kept[0].line == 1
    assert len(review) == 1
    assert review[0].possible_gops == ["01210"]
    assert "Abrechnungsausschluss" in review[0].reason
    # Ein Ausschluss ist keine Dokumentationsluecke - dafuer gibt es nichts nachzutragen.
    assert hints == []


def test_ignored_clause_types_do_not_reach_the_gate() -> None:
    """Berichtspflicht ist Verwaltungsangabe, keine Abrechnungsbedingung."""
    policy = load_billing_rule_set().clause_policy
    assert "reporting" in (policy.get("ignored_clause_types") or [])


def content_clause(*elements: str) -> dict:
    return {
        "clause_type": "required_service_content",
        "scope": "service",
        "parameters": {"elements": list(elements)},
        "source_text": ", ".join(elements),
    }


def test_uncovered_obligatory_content_is_reported_but_does_not_block() -> None:
    """Meldemodus: die Lücke steht in der Position, verhindert sie aber nicht."""
    current = item("99999", "2026-01-03", "session-1", 1)
    verdict = _evaluate_clause(content_clause("Dokumentation im Mutterpass"), current, Counter(), {}, {})

    assert verdict is not None
    assert verdict.severity == ADVISORY
    assert "nicht vollständig belegt" in verdict.note


def test_uncovered_obligatory_content_blocks_when_the_policy_says_so() -> None:
    current = item("99999", "2026-01-03", "session-1", 1)
    policy = {"required_service_content_blocks": True}

    verdict = _evaluate_clause(content_clause("Dokumentation im Mutterpass"), current, Counter(), {}, policy)

    assert verdict is not None
    assert verdict.severity == VIOLATION


def test_covered_obligatory_content_passes_despite_different_wording() -> None:
    """Das Modell darf kürzen und umstellen, solange die tragenden Wörter stimmen."""
    current = item("99999", "2026-01-03", "session-1", 1)
    current.covered_service_content = ["Dokumentation der Befunde im Mutterpass der Patientin"]

    assert _evaluate_clause(content_clause("Dokumentation im Mutterpass"), current, Counter(), {}, {}) is None


def test_partially_covered_content_still_names_the_missing_element() -> None:
    current = item("99999", "2026-01-03", "session-1", 1)
    current.covered_service_content = ["Ultraschalluntersuchungen nach Anlage I a"]

    verdict = _evaluate_clause(
        content_clause("Ultraschalluntersuchungen nach Anlage I a", "Dokumentation im Mutterpass"),
        current,
        Counter(),
        {},
        {},
    )

    assert verdict is not None
    assert "Mutterpass" in verdict.note
    assert "Ultraschall" not in verdict.note


def authorization_clause(agreement: str) -> dict:
    return {
        "clause_type": "requires_authorization",
        "scope": "provider",
        "parameters": {"agreement": agreement},
        "source_text": f"setzt eine Genehmigung nach der {agreement} voraus",
    }


def test_unbacked_authorization_keeps_the_position_out_of_the_invoice():
    """Ob die Betriebsstätte die Genehmigung hat, steht nicht in der Akte.

    Ohne ausdrückliche Erklärung wird die Position vorgelegt statt abgerechnet:
    eine vorhandene Genehmigung kostet eine Bestätigung, eine fehlende sonst eine
    Falschabrechnung.
    """
    current = item("99999", "2026-01-03", "session-1", 1)

    verdict = _evaluate_clause(authorization_clause("Ultraschall-Vereinbarung"), current, Counter(), {}, {})

    assert verdict is not None
    assert verdict.severity == VIOLATION
    assert "Ultraschall-Vereinbarung" in verdict.note


def test_declared_authorization_lets_the_position_pass(monkeypatch):
    import app.catalog_rule_validation as validation

    monkeypatch.setattr(validation, "_declared_authorizations", lambda: ("Ultraschall-Vereinbarung",))
    current = item("99999", "2026-01-03", "session-1", 1)

    assert _evaluate_clause(authorization_clause("Ultraschall-Vereinbarung"), current, Counter(), {}, {}) is None


def test_a_short_quote_covers_the_longer_requirement_it_comes_from():
    """Ein kurzes, korrektes Zitat muss eine lange Anforderung belegen können.

    Am Produktionsentwurf gemessen: Der belegte "Persönliche Arzt-Patienten-Kontakt
    im organisierten Not(-fall)dienst" deckte 0,58 der Anforderung ab, die denselben
    Satz um die Aufzählung der Leistungserbringer verlängert — knapp unter der
    Schwelle. Die Position wurde als unvollständig dokumentiert gemeldet, obwohl
    jedes Wort des Belegs in der Anforderung stand. Der Arzt hätte etwas
    nachgetragen, das bereits dasteht.
    """
    from app.catalog_rule_validation import _uncovered_content

    anforderung = (
        "Persönlicher Arzt-Patienten-Kontakt im organisierten Not(-fall)dienst und für "
        "nicht an der vertragsärztlichen Versorgung teilnehmende Ärzte"
    )
    beleg = "Persönlicher Arzt-Patienten-Kontakt im organisierten Not(-fall)dienst"

    assert _uncovered_content([anforderung], [beleg]) == []

    # Umgekehrt bleibt die Prüfung streng: Ein Beleg, der nur zufällig ein paar
    # Wörter teilt, deckt die Anforderung nicht.
    fremd = "Dokumentation im Mutterpass nach Richtlinie"
    assert _uncovered_content([anforderung], [fremd]) == [anforderung]

    # Und ein Beleg ohne tragende Schnittmenge belegt gar nichts.
    assert _uncovered_content(["Pulsoxymetrie"], ["Befundbesprechung"]) == ["Pulsoxymetrie"]
