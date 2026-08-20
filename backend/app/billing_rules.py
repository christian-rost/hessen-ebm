from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any
from dataclasses import dataclass
from datetime import date, time, timedelta


@dataclass(frozen=True)
class BillingRuleContext:
    gop: str
    service_date: str | None = None
    service_time: str | None = None
    region: str = "Hessen"
    evidence_kind: str | None = None
    evidence_text: str = ""
    evidence_metadata: Mapping[str, Any] | None = None
    catalog_rule_texts: Sequence[str] = ()


@dataclass(frozen=True)
class GopRuleDecision:
    gop: str | None
    rule_id: str
    notes: tuple[str, ...] = ()
    review_required: bool = False


TIME_DEPENDENT_EMERGENCY_GOPS = {"01205", "01207", "01210", "01212", "01214", "01216", "01218"}
KV_NOTFALL_ZNA_KIND = "context.kv_notfall_zna"

REVIEW_RULE_DIMENSIONS = {
    "time": "Uhrzeit",
    "age": "Alter",
    "sex": "Geschlecht/Schwangerschaft",
    "diagnosis": "Diagnose/ICD",
    "frequency": "Häufigkeit",
    "exclusion": "Nebeneinanderberechnung/Ausschluss",
}


def candidate_gops_for_evidence_kind(evidence_kind: str) -> list[str]:
    if evidence_kind == KV_NOTFALL_ZNA_KIND:
        return ["01210", "01212"]
    return []


def resolve_evidence_rule_gop(
    evidence_kind: str,
    fallback_gop: str,
    service_date: str | None,
    service_time: str | None,
    region: str = "Hessen",
) -> GopRuleDecision:
    if evidence_kind == KV_NOTFALL_ZNA_KIND:
        decision = evaluate_gop_rules(
            BillingRuleContext(
                gop=fallback_gop,
                service_date=service_date,
                service_time=service_time,
                region=region,
                evidence_kind=evidence_kind,
            )
        )
        if decision.gop:
            return decision
        return GopRuleDecision(
            fallback_gop,
            decision.rule_id,
            decision.notes,
            review_required=True,
        )

    return GopRuleDecision(fallback_gop, f"static.{fallback_gop}.v1")


def evaluate_gop_rules(context: BillingRuleContext) -> GopRuleDecision:
    decisions: list[GopRuleDecision] = []
    decisions.append(apply_temporal_gop_rule(context.gop, context.service_date, context.service_time, context.region))
    decisions.append(evaluate_catalog_context_rules(context))
    return _combine_decisions(context.gop, decisions)


def apply_temporal_gop_rule(
    gop: str,
    service_date: str | None,
    service_time: str | None,
    region: str = "Hessen",
) -> GopRuleDecision:
    normalized = gop.strip().upper()
    if normalized not in TIME_DEPENDENT_EMERGENCY_GOPS:
        return GopRuleDecision(normalized, f"static.{normalized}.v1")

    if normalized in {"01210", "01212"}:
        decision = emergency_initial_gop(service_date, service_time, region)
        group = "Notfall-Erstkontakt"
    elif normalized in {"01214", "01216", "01218"}:
        decision = emergency_consultation_gop(service_date, service_time, region)
        group = "Notfall-Konsultation"
    else:
        decision = emergency_clarification_gop(service_date, service_time, region)
        group = "Notfall-Abklärung"

    if not decision.gop:
        return GopRuleDecision(
            normalized,
            decision.rule_id,
            decision.notes,
            review_required=True,
        )
    if decision.gop != normalized:
        return GopRuleDecision(
            decision.gop,
            decision.rule_id,
            (
                f"Zeitregel {group}: GOP {normalized} wurde anhand von Datum/Uhrzeit auf {decision.gop} korrigiert.",
                *decision.notes,
            ),
            review_required=decision.review_required,
        )
    return decision


def evaluate_catalog_context_rules(context: BillingRuleContext) -> GopRuleDecision:
    rule_text = _combined_catalog_rule_text(context.catalog_rule_texts)
    if not rule_text:
        return GopRuleDecision(None, "catalog.context.noop.v1")

    metadata = context.evidence_metadata or {}
    notes: list[str] = []

    if _requires_time(rule_text) and not context.service_time:
        notes.append("Katalogregel verlangt eine Uhrzeit; in der Evidenz wurde keine Uhrzeit gefunden.")
    if _requires_age(rule_text) and not _has_any(metadata, "patient_age", "age", "birth_date", "birthdate", "geburtsdatum"):
        notes.append("Katalogregel enthält eine Altersbedingung; Alter oder Geburtsdatum fehlen im strukturierten Kontext.")
    if _requires_sex_or_pregnancy(rule_text) and not _has_any(
        metadata,
        "patient_sex",
        "patient_gender",
        "sex",
        "gender",
        "pregnancy",
        "pregnant",
        "schwangerschaft",
    ):
        notes.append("Katalogregel enthält Geschlechts- oder Schwangerschaftsbezug; der strukturierte Kontext enthält dazu keinen sicheren Wert.")
    if _requires_diagnosis(rule_text) and not _has_any(metadata, "diagnosis", "diagnoses", "icd10", "icd"):
        notes.append("Katalogregel enthält Diagnose- oder ICD-Bezug; im strukturierten Kontext fehlt eine gesicherte Diagnosezuordnung.")
    if _requires_frequency_check(rule_text):
        notes.append("Katalogregel enthält eine Häufigkeitsbegrenzung; Fall-/Quartalszählung muss regelbasiert geprüft werden.")
    if _requires_exclusion_check(rule_text):
        notes.append("Katalogregel enthält Ausschlüsse oder Nebeneinanderberechnung; andere Positionen des Falls müssen geprüft werden.")

    if not notes:
        return GopRuleDecision(None, "catalog.context.checked.v1")
    return GopRuleDecision(
        None,
        "catalog.context.review.v1",
        tuple(notes),
        review_required=True,
    )


def billing_rule_guidance() -> dict[str, Any]:
    return {
        "rule_layer": {
            "principle": (
                "Jede vorgeschlagene GOP wird nach der semantischen Herleitung regelbasiert geprüft. "
                "Die Regelschicht darf GOPs korrigieren, übernehmen oder zur manuellen Prüfung markieren."
            ),
            "dimensions": REVIEW_RULE_DIMENSIONS,
        },
        "time_dependent_emergency_gops": {
            "01210_01212": (
                "Für den Notfall-Erstkontakt ist 01210 werktags von 07:00 Uhr bis vor 19:00 Uhr zu verwenden. "
                "01212 gilt am Wochenende, an Feiertagen, am 24.12./31.12. und außerhalb 07:00-19:00 Uhr."
            ),
            "01214_01216_01218": (
                "Für weitere Konsultationen im Notfall ist 01214 werktags 07:00-19:00 Uhr, "
                "01216 werktags 19:00-22:00 Uhr sowie an Wochenenden/Feiertagen/Sondertagen 07:00-19:00 Uhr, "
                "und 01218 nachts bzw. an Wochenenden/Feiertagen/Sondertagen außerhalb 07:00-19:00 Uhr zu verwenden."
            ),
            "01205_01207": (
                "Für Notfallabklärung ist 01205 werktags 07:00-19:00 Uhr und 01207 am Wochenende, "
                "an Feiertagen, am 24.12./31.12. oder außerhalb 07:00-19:00 Uhr zu verwenden."
            ),
            "missing_datetime": "Wenn Datum oder Uhrzeit fehlen, keine sichere zeitabhängige Notfall-GOP festlegen, sondern als Review-Kandidat markieren.",
        },
    }


def emergency_initial_gop(
    service_date: str | None,
    service_time: str | None,
    region: str = "Hessen",
) -> GopRuleDecision:
    parsed = _parse_rule_datetime(service_date, service_time)
    if parsed is None:
        return _missing_datetime_decision("time.notfall.initial.missing.v1")
    day, clock = parsed
    if is_special_notfall_day(day, region) or not _is_daytime(clock):
        return GopRuleDecision(
            "01212",
            "time.notfall.initial.01212.v1",
            ("Notfallpauschale II: Wochenende/Feiertag/Sondertag oder außerhalb 07:00-19:00 Uhr.",),
        )
    return GopRuleDecision(
        "01210",
        "time.notfall.initial.01210.v1",
        ("Notfallpauschale I: Werktag von 07:00 Uhr bis vor 19:00 Uhr.",),
    )


def emergency_consultation_gop(
    service_date: str | None,
    service_time: str | None,
    region: str = "Hessen",
) -> GopRuleDecision:
    parsed = _parse_rule_datetime(service_date, service_time)
    if parsed is None:
        return _missing_datetime_decision("time.notfall.consultation.missing.v1")
    day, clock = parsed
    special_day = is_special_notfall_day(day, region)

    if special_day:
        if _is_daytime(clock):
            return GopRuleDecision(
                "01216",
                "time.notfall.consultation.01216.v1",
                ("Konsultation im Notfall: Wochenende/Feiertag/Sondertag von 07:00 Uhr bis vor 19:00 Uhr.",),
            )
        return GopRuleDecision(
            "01218",
            "time.notfall.consultation.01218.v1",
            ("Konsultation im Notfall: Wochenende/Feiertag/Sondertag außerhalb 07:00-19:00 Uhr.",),
        )

    if time(19, 0) <= clock < time(22, 0):
        return GopRuleDecision(
            "01216",
            "time.notfall.consultation.01216.v1",
            ("Konsultation im Notfall: Werktag von 19:00 Uhr bis vor 22:00 Uhr.",),
        )
    if clock >= time(22, 0) or clock < time(7, 0):
        return GopRuleDecision(
            "01218",
            "time.notfall.consultation.01218.v1",
            ("Konsultation im Notfall: Werktag von 22:00 Uhr bis vor 07:00 Uhr.",),
        )
    return GopRuleDecision(
        "01214",
        "time.notfall.consultation.01214.v1",
        ("Konsultation im Notfall: Werktag von 07:00 Uhr bis vor 19:00 Uhr.",),
    )


def emergency_clarification_gop(
    service_date: str | None,
    service_time: str | None,
    region: str = "Hessen",
) -> GopRuleDecision:
    parsed = _parse_rule_datetime(service_date, service_time)
    if parsed is None:
        return _missing_datetime_decision("time.notfall.clarification.missing.v1")
    day, clock = parsed
    if is_special_notfall_day(day, region) or not _is_daytime(clock):
        return GopRuleDecision(
            "01207",
            "time.notfall.clarification.01207.v1",
            ("Notfallabklärung: Wochenende/Feiertag/Sondertag oder außerhalb 07:00-19:00 Uhr.",),
        )
    return GopRuleDecision(
        "01205",
        "time.notfall.clarification.01205.v1",
        ("Notfallabklärung: Werktag von 07:00 Uhr bis vor 19:00 Uhr.",),
    )


def is_special_notfall_day(service_date: str | date, region: str = "Hessen") -> bool:
    day = service_date if isinstance(service_date, date) else _parse_date(service_date)
    if day is None:
        return False
    special_dates = (
        hessen_public_holidays(day.year) if region.strip().lower() == "hessen" else german_core_public_holidays(day.year)
    )
    return day.weekday() >= 5 or day in special_dates or (day.month, day.day) in {(12, 24), (12, 31)}


def hessen_public_holidays(year: int) -> set[date]:
    holidays = german_core_public_holidays(year)
    easter = _easter_sunday(year)
    holidays.add(easter + timedelta(days=60))  # Fronleichnam
    return holidays


def german_core_public_holidays(year: int) -> set[date]:
    easter = _easter_sunday(year)
    return {
        date(year, 1, 1),
        easter - timedelta(days=2),
        easter + timedelta(days=1),
        date(year, 5, 1),
        easter + timedelta(days=39),
        easter + timedelta(days=50),
        date(year, 10, 3),
        date(year, 12, 25),
        date(year, 12, 26),
    }


def _missing_datetime_decision(rule_id: str) -> GopRuleDecision:
    return GopRuleDecision(
        None,
        rule_id,
        ("Datum oder Uhrzeit fehlt; zeitabhängige Notfall-GOP muss manuell geprüft werden.",),
        review_required=True,
    )


def _parse_rule_datetime(service_date: str | None, service_time: str | None) -> tuple[date, time] | None:
    day = _parse_date(service_date)
    clock = _parse_time(service_time)
    if day is None or clock is None:
        return None
    return day, clock


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    cleaned = value.strip().split()[0]
    parts = cleaned.split(":")
    try:
        if len(parts) >= 2:
            return time(int(parts[0]), int(parts[1][:2]))
        return time.fromisoformat(cleaned[:5])
    except (ValueError, IndexError):
        return None


def _is_daytime(clock: time) -> bool:
    return time(7, 0) <= clock < time(19, 0)


def _combine_decisions(original_gop: str, decisions: list[GopRuleDecision]) -> GopRuleDecision:
    current_gop = original_gop.strip().upper()
    rule_ids: list[str] = []
    notes: list[str] = []
    review_required = False

    for decision in decisions:
        if decision.rule_id != "catalog.context.noop.v1":
            rule_ids.append(decision.rule_id)
        notes.extend(decision.notes)
        review_required = review_required or decision.review_required
        if decision.gop:
            current_gop = decision.gop

    return GopRuleDecision(
        current_gop,
        "+".join(rule_ids) or "rules.noop.v1",
        tuple(dict.fromkeys(notes)),
        review_required=review_required,
    )


def _combined_catalog_rule_text(rule_texts: Sequence[str]) -> str:
    return " ".join(str(text) for text in rule_texts if str(text).strip()).lower()


def _has_any(metadata: Mapping[str, Any], *keys: str) -> bool:
    for key in keys:
        value = metadata.get(key)
        if value not in (None, "", [], {}):
            return True
    return False


def _requires_time(rule_text: str) -> bool:
    return bool(re.search(r"\b(uhrzeit|inanspruchnahme.*uhr|zwischen\s+\d{1,2}[:.]?\d{0,2}\s+und\s+\d{1,2})\b", rule_text))


def _requires_age(rule_text: str) -> bool:
    return bool(
        re.search(
            r"\b(lebensjahr|alter|alters|säugling|kleinkind|kind(er)?|jugendlich|erwachsen|geburtstag|vollendet)\b",
            rule_text,
        )
    )


def _requires_sex_or_pregnancy(rule_text: str) -> bool:
    return bool(
        re.search(
            r"\b(schwangerschaft|schwanger|geburtshilfe|geburt|weiblich|männlich|frau(en)?|mann|männer|prostata|uterus|mamma)\b",
            rule_text,
        )
    )


def _requires_diagnosis(rule_text: str) -> bool:
    return bool(re.search(r"\b(icd|diagnose|behandlungsdiagnose|gesicherte diagnose|erkrankung)\b", rule_text))


def _requires_frequency_check(rule_text: str) -> bool:
    return bool(re.search(r"\b(einmal|höchstens|maximal|je behandlungsfall|im krankheitsfall|im quartal|nicht mehrfach)\b", rule_text))


def _requires_exclusion_check(rule_text: str) -> bool:
    return bool(re.search(r"\b(nicht neben|nebeneinander|ausschluss|nicht berechnungsfähig|nicht abrechnungsfähig)\b", rule_text))


def _easter_sunday(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)
