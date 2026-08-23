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


def test_precision_holds_recall_honest():
    """Konzept 6: Recall allein laesst sich durch mehr Vorschlaege hochtreiben.

    Zwei Faelle mit gleicher Trefferzahl, aber verschieden vielen Fehlvorschlaegen
    muessen sich in der Kennzahl unterscheiden - sonst belohnt der Messstand
    genau das Verhalten, das die Rechnung unbrauchbar macht.
    """
    from tools.measure_cases import CaseResult, _totals

    sauber = CaseResult(name="sauber", derived=True, expected=["A@1", "B@1"], hit=["A@1"], missing=["B@1"])
    streuend = CaseResult(
        name="streuend", derived=True, expected=["A@1", "B@1"], hit=["A@1"], missing=["B@1"],
        extra=["X@1", "Y@1", "Z@1"],
    )
    assert sauber.precision == 1.0
    assert streuend.precision == 0.25
    assert sauber.recall == streuend.recall

    gesamt = _totals([sauber, streuend])
    assert gesamt["trefferquote"] == 0.5
    assert gesamt["precision"] == 0.4


def test_hint_turns_a_silent_loss_into_a_visible_one():
    """Eine uebersehene Position, die als Hinweis erscheint, ist kein stiller Ausfall."""
    from tools.measure_cases import CaseResult, _totals

    still = CaseResult(name="still", derived=True, expected=["A@2026-01-01"], missing=["A@2026-01-01"])
    sichtbar = CaseResult(
        name="sichtbar", derived=True, expected=["A@2026-01-01"], missing=["A@2026-01-01"],
        hints=["A@2026-01-01"],
    )
    assert still.recovered_by_hint == []
    assert sichtbar.recovered_by_hint == ["A@2026-01-01"]
    assert _totals([still])["still_verloren"] == 1
    assert _totals([sichtbar])["still_verloren"] == 0
