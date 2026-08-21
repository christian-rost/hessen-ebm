from collections import Counter

from app.catalog_rule_validation import _counts_for_scope, _evaluate_clause
from app.models import BillingItem


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

    note = _evaluate_clause(clause, current, Counter({"99999": 1}), {})

    assert note is not None
    assert "patientenbezogene Abrechnungshistorie" in note
