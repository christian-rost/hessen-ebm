"""Der Messstand ist die Grundlage jeder weiteren Entscheidung.

Rechnet er falsch, wird auf falscher Grundlage entschieden — deshalb wird seine
Arithmetik geprüft, nicht nur sein Durchlauf.
"""

from tools.measure_cases import CaseResult, _key, _totals


def test_position_key_keeps_the_suffix():
    """32035A und 32035 sind auf der Rechnung verschiedene Angaben."""
    assert _key("32035A", "2026-01-01") != _key("32035", "2026-01-01")
    assert _key(" 01212 ", "2026-01-01T13:05") == "01212@2026-01-01"


def test_same_gop_on_different_days_counts_twice():
    assert _key("01786", "2026-01-01") != _key("01786", "2026-01-03")


def case(name, expected, produced, derived=True):
    result = CaseResult(name=name, derived=derived, expected=expected, produced=produced)
    result.hit = sorted(set(expected) & set(produced))
    result.missing = sorted(set(expected) - set(produced))
    result.extra = sorted(set(produced) - set(expected))
    return result


def test_totals_separate_missing_from_additional():
    soll = ["01212@2026-01-01", "01786@2026-01-01", "33042@2026-01-01"]
    ist = ["01212@2026-01-01", "01786@2026-01-01", "01770@2026-01-01"]

    totals = _totals([case("a", soll, ist)])

    assert totals["soll"] == 3
    assert totals["treffer"] == 2
    assert totals["fehlend"] == 1
    assert totals["zusaetzlich"] == 1
    assert totals["trefferquote"] == round(2 / 3, 4)


def test_a_case_without_derivation_counts_as_missed_not_as_absent():
    """Ein Fall ohne Ableitung darf die Quote nicht schönen, indem er herausfällt."""
    soll = ["01212@2026-01-01", "01786@2026-01-01"]

    totals = _totals([case("a", soll, [], derived=False)])

    assert totals["faelle"] == 1
    assert totals["abgeleitet"] == 0
    assert totals["soll"] == 2
    assert totals["trefferquote"] == 0.0


def test_recall_is_reported_per_case():
    result = case("a", ["01212@2026-01-01", "01786@2026-01-01"], ["01212@2026-01-01"])
    assert result.recall == 0.5
    assert result.as_dict()["fehlend"] == ["01786@2026-01-01"]
