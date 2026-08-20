from app.billing_rules import (
    BillingRuleContext,
    apply_temporal_gop_rule,
    evaluate_catalog_context_rules,
    evaluate_gop_rules,
    emergency_clarification_gop,
    emergency_consultation_gop,
    emergency_initial_gop,
    is_special_notfall_day,
)


def test_emergency_initial_gop_uses_weekday_daytime_01210():
    decision = emergency_initial_gop("2026-04-24", "12:20")

    assert decision.gop == "01210"
    assert decision.review_required is False


def test_emergency_initial_gop_uses_01212_at_night_weekend_and_special_days():
    assert emergency_initial_gop("2026-04-24", "20:00").gop == "01212"
    assert emergency_initial_gop("2026-04-25", "12:00").gop == "01212"
    assert emergency_initial_gop("2026-12-24", "12:00").gop == "01212"
    assert emergency_initial_gop("2026-06-04", "12:00").gop == "01212"


def test_missing_datetime_requires_review_for_time_dependent_emergency_gops():
    decision = emergency_initial_gop("2026-04-24", None)

    assert decision.gop is None
    assert decision.review_required is True
    assert "Datum oder Uhrzeit fehlt" in decision.notes[0]


def test_emergency_consultation_gop_uses_time_windows():
    assert emergency_consultation_gop("2026-04-24", "12:20").gop == "01214"
    assert emergency_consultation_gop("2026-04-24", "20:00").gop == "01216"
    assert emergency_consultation_gop("2026-04-24", "23:00").gop == "01218"
    assert emergency_consultation_gop("2026-04-25", "12:00").gop == "01216"
    assert emergency_consultation_gop("2026-04-25", "20:00").gop == "01218"


def test_emergency_clarification_gop_uses_daytime_pair():
    assert emergency_clarification_gop("2026-04-24", "12:20").gop == "01205"
    assert emergency_clarification_gop("2026-04-24", "20:00").gop == "01207"
    assert emergency_clarification_gop("2026-04-25", "12:00").gop == "01207"


def test_apply_temporal_gop_rule_corrects_wrong_emergency_pair():
    decision = apply_temporal_gop_rule("01210", "2026-04-24", "20:00")

    assert decision.gop == "01212"
    assert "korrigiert" in decision.notes[0]


def test_special_notfall_day_includes_hessen_public_holiday():
    assert is_special_notfall_day("2026-06-04", "Hessen") is True


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
