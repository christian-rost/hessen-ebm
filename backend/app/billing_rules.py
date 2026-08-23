from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any

from .billing_rule_definitions import (
    BillingRuleSet,
    CandidateRuleDefinition,
    DerivedRuleDefinition,
    EventSequenceRuleDefinition,
    TemporalRuleDefinition,
    definition_is_applicable,
)
from .billing_rule_store import get_runtime_billing_rule_set, get_runtime_clinical_definition_set
from .clinical_definitions import kinds_with_flags


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


@dataclass(frozen=True)
class DerivedGopDecision:
    gop: str | None
    rule_id: str
    evidence_ids: tuple[str, ...] = ()
    evidence_pages: tuple[int, ...] = ()
    notes: tuple[str, ...] = ()
    review_required: bool = False
    metadata: Mapping[str, Any] | None = None
    target_gop: str | None = None
    insert_after: str | None = None
    title_hint: str | None = None
    evidence_kind: str | None = None


REVIEW_RULE_DIMENSIONS = {
    "time": "Uhrzeit",
    "age": "Alter",
    "sex": "Geschlecht/Schwangerschaft",
    "diagnosis": "Diagnose/ICD",
    "frequency": "Häufigkeit",
    "exclusion": "Nebeneinanderberechnung/Ausschluss",
}


@dataclass(frozen=True)
class _RuleFacts:
    gops: frozenset[str]
    evidence: tuple[Mapping[str, Any], ...]
    service_date: str | None
    service_time: str | None
    region: str
    quarter: str | None
    text: str
    diagnoses: tuple[str, ...]
    patient_age: int | None
    metadata: Mapping[str, tuple[Any, ...]]


def candidate_gops_for_evidence_kind(
    evidence_kind: str,
    quarter: str | None = None,
    region: str = "Hessen",
    rule_set: BillingRuleSet | None = None,
    evidence_flags: frozenset[str] = frozenset(),
) -> list[str]:
    """GOP-Kandidaten einer Evidenzart.

    `evidence_flags` sind die Metadatenmerkmale der konkreten Evidenz. Eine
    Sequenzregel greift, wenn die Art genannt ist oder eines ihrer Merkmale
    passt; damit erreichen Kontaktpauschalen auch neue Evidenzarten.
    """
    definitions = rule_set or get_runtime_billing_rule_set(quarter, region)
    configured_candidates = [
        gop
        for rule in definitions.candidate_rules
        if rule.evidence_kind == evidence_kind and _definition_applies(rule, quarter, region)
        for gop in rule.gops
    ]
    sequence_candidates = [
        gop
        for rule in definitions.event_sequence_rules
        if (
            evidence_kind in rule.evidence_kinds
            or (evidence_flags & frozenset(rule.evidence_flags))
            or evidence_kind in kinds_with_flags(get_runtime_clinical_definition_set(), rule.evidence_flags)
        )
        and _definition_applies(rule, quarter, region)
        for gop in (rule.initial_gop, rule.subsequent_gop)
    ]
    candidates = configured_candidates + sequence_candidates
    direct_bases = {_normalize_rule_gop(gop) for gop in candidates}
    for rule in definitions.temporal_rules:
        if not _definition_applies(rule, quarter, region):
            continue
        if direct_bases.intersection({_normalize_rule_gop(gop) for gop in rule.gops}):
            candidates.extend(outcome.gop for outcome in rule.outcomes)
    return list(dict.fromkeys(candidates))


def resolve_evidence_rule_gop(
    evidence_kind: str,
    fallback_gop: str,
    service_date: str | None,
    service_time: str | None,
    region: str = "Hessen",
    quarter: str | None = None,
    rule_set: BillingRuleSet | None = None,
) -> GopRuleDecision:
    decision = apply_temporal_gop_rule(
        fallback_gop,
        service_date,
        service_time,
        region,
        quarter=quarter,
        rule_set=rule_set,
    )
    if decision.gop:
        return decision
    return GopRuleDecision(
        _normalize_display_gop(fallback_gop),
        decision.rule_id,
        decision.notes,
        review_required=True,
    )


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
    quarter: str | None = None,
    rule_set: BillingRuleSet | None = None,
) -> GopRuleDecision:
    normalized = _normalize_display_gop(gop)
    match = re.fullmatch(r"(\d{5})([A-Z0-9*]+)?", normalized)
    if match and match.group(2):
        return GopRuleDecision(normalized, f"static.{normalized}.v1")
    normalized = match.group(1) if match else normalized
    effective_quarter = quarter or _quarter_from_date(service_date)
    definitions = rule_set or get_runtime_billing_rule_set(effective_quarter, region)
    temporal_rule = next(
        (
            rule
            for rule in definitions.temporal_rules
            if normalized in {_normalize_rule_gop(value) for value in rule.gops}
            and _definition_applies(rule, effective_quarter, region)
        ),
        None,
    )
    if temporal_rule is None:
        return GopRuleDecision(normalized, f"static.{normalized}.v1")

    missing = [
        field
        for field in temporal_rule.required_context
        if not {"service_date": service_date, "service_time": service_time, "region": region}.get(field)
    ]

    facts = _facts((), (), service_date, service_time, region, effective_quarter)
    if missing:
        # Fehlender Kontext blockiert nicht pauschal: manche Ergebnisse sind allein
        # aus dem Datum entscheidbar, etwa eine Pauschale, die an einem Feiertag
        # unabhaengig von der Uhrzeit gilt. Nur wenn kein Ergebnis eindeutig
        # zutrifft, geht die GOP in die manuelle Pruefung.
        decidable = [outcome for outcome in temporal_rule.outcomes if _matches_condition(outcome.when, facts)]
        if len(decidable) == 1:
            outcome = decidable[0]
            notes = (outcome.note,)
            if outcome.gop != normalized:
                notes = (
                    f"Zeitregel {temporal_rule.name}: GOP {normalized} wurde anhand des Leistungsdatums auf "
                    f"{outcome.gop} korrigiert.",
                    outcome.note,
                )
            return GopRuleDecision(outcome.gop, outcome.rule_id, notes)
        labels = {"service_date": "Datum", "service_time": "Uhrzeit", "region": "Region"}
        missing_labels = ", ".join(labels.get(field, field) for field in missing)
        return GopRuleDecision(
            None,
            f"{temporal_rule.rule_id}.missing",
            (
                f"{missing_labels} fehlt und das Leistungsdatum allein ergibt keine eindeutige Variante; "
                "die zeitabhängige GOP muss manuell geprüft werden."
            ,),
            review_required=True,
        )
    for outcome in temporal_rule.outcomes:
        if not _matches_condition(outcome.when, facts):
            continue
        notes = (outcome.note,)
        if outcome.gop != normalized:
            notes = (
                f"Zeitregel {temporal_rule.name}: GOP {normalized} wurde anhand von Datum/Uhrzeit auf {outcome.gop} korrigiert.",
                outcome.note,
            )
        return GopRuleDecision(outcome.gop, outcome.rule_id, notes)

    return GopRuleDecision(
        normalized,
        f"{temporal_rule.rule_id}.unmatched",
        (f"Für die Zeitregel {temporal_rule.name} konnte kein eindeutiges Ergebnis ermittelt werden.",),
        review_required=True,
    )


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


def billing_rule_guidance(rule_set: BillingRuleSet | None = None) -> dict[str, Any]:
    definitions = rule_set or get_runtime_billing_rule_set()
    return {
        "rule_set": {
            "id": definitions.rule_set_id,
            "version": definitions.version,
            "schema_version": definitions.schema_version,
        },
        "rule_layer": {
            "principle": (
                "Jede vorgeschlagene GOP wird nach der semantischen Herleitung regelbasiert geprüft. "
                "Die Regelschicht darf GOPs korrigieren, übernehmen oder zur manuellen Prüfung markieren."
            ),
            "dimensions": REVIEW_RULE_DIMENSIONS,
        },
        "candidate_rules": [
            {
                "rule_id": rule.rule_id,
                "evidence_kind": rule.evidence_kind,
                "gops": list(rule.gops),
            }
            for rule in definitions.candidate_rules
        ],
        "temporal_rules": [
            {
                "rule_id": rule.rule_id,
                "name": rule.name,
                "gops": list(rule.gops),
                "outcomes": [{"gop": outcome.gop, "note": outcome.note} for outcome in rule.outcomes],
            }
            for rule in definitions.temporal_rules
        ],
        "event_sequence_rules": [
            {
                "rule_id": rule.rule_id,
                "name": rule.name,
                "evidence_kinds": list(rule.evidence_kinds),
                "initial_gop": rule.initial_gop,
                "subsequent_gop": rule.subsequent_gop,
                "session_gap_minutes": rule.session_gap_minutes,
            }
            for rule in definitions.event_sequence_rules
        ],
        "derived_rules": [
            {
                "rule_id": rule.rule_id,
                "gop": rule.gop,
                "description": rule.description,
                "criteria": [criterion.label for criterion in rule.criteria],
                "exclusions": [exclusion.label for exclusion in rule.exclusions],
            }
            for rule in definitions.derived_rules
        ],
    }


def derive_additional_gops(
    existing_gops: Sequence[str],
    evidence: Sequence[Mapping[str, Any]],
    quarter: str | None = None,
    region: str = "Hessen",
    rule_set: BillingRuleSet | None = None,
) -> list[DerivedGopDecision]:
    definitions = rule_set or get_runtime_billing_rule_set(quarter, region)
    normalized_gops = {_normalize_rule_gop(gop) for gop in existing_gops}
    decisions: list[DerivedGopDecision] = []

    # Regeln dürfen auf zuvor abgeleiteten GOPs aufbauen. Der Fixpunkt ist durch
    # die endliche Zahl eindeutiger Ziel-GOPs begrenzt.
    pending = list(definitions.derived_rules)
    while pending:
        matched_in_pass = False
        for rule in list(pending):
            target = _normalize_rule_gop(rule.gop)
            if target in normalized_gops or not _definition_applies(rule, quarter, region):
                pending.remove(rule)
                continue
            facts = _facts(normalized_gops, evidence, None, None, region, quarter)
            if not all(_matches_condition(condition, facts) for condition in rule.requirements):
                continue

            matched_criteria = [
                criterion.label for criterion in rule.criteria if _matches_condition(criterion.when, facts)
            ]
            if rule.criteria and not matched_criteria:
                pending.remove(rule)
                continue

            exclusions = [
                exclusion for exclusion in rule.exclusions if _matches_condition(exclusion.when, facts)
            ]
            if exclusions:
                pending.remove(rule)
                continue

            supporting = _supporting_evidence(rule, evidence, normalized_gops, region, quarter)
            evidence_ids = tuple(
                dict.fromkeys(str(item.get("evidence_id")) for item in supporting if item.get("evidence_id"))
            )
            evidence_pages = tuple(
                sorted({int(item.get("page")) for item in supporting if str(item.get("page") or "").isdigit()})
            )
            notes = (
                f"{rule.gop} ergänzt: {', '.join(matched_criteria)}."
                if matched_criteria
                else f"{rule.gop} gemäß Regel {rule.rule_id} ergänzt."
            ,)
            decision = DerivedGopDecision(
                gop=rule.gop,
                target_gop=rule.gop,
                rule_id=rule.rule_id,
                evidence_ids=evidence_ids,
                evidence_pages=evidence_pages,
                notes=notes,
                metadata={
                    "diagnoses": list(facts.diagnoses),
                    "patient_age": facts.patient_age,
                    "derivation_criteria": matched_criteria,
                    "rule_set_id": definitions.rule_set_id,
                    "rule_set_version": definitions.version,
                },
                insert_after=rule.insert_after,
                title_hint=rule.title_hint,
                evidence_kind=rule.evidence_kind,
            )
            decisions.append(decision)
            normalized_gops.add(target)
            pending.remove(rule)
            matched_in_pass = True
        if not matched_in_pass:
            break
    return decisions


def is_special_calendar_day(
    service_date: str | date,
    region: str = "Hessen",
    rule_set: BillingRuleSet | None = None,
) -> bool:
    """Prueft den Tag gegen den Kalender aus `calendar_definitions` des Regelwerks.

    Wochentage, feste Datumsangaben und Osterabstaende stehen in den Definitionen,
    nicht im Code; die Funktion bleibt daher frei von Fach- und Regionalwissen.
    """
    day = service_date if isinstance(service_date, date) else _parse_date(service_date)
    if day is None:
        return False
    definitions = (rule_set or get_runtime_billing_rule_set()).calendar_definitions
    calendar = _calendar_for_region(definitions, region)
    weekdays = {int(value) for value in calendar.get("weekdays") or []}
    if day.weekday() in weekdays:
        return True
    if day.strftime("%m-%d") in {str(value) for value in calendar.get("fixed_dates") or []}:
        return True
    easter = _easter_sunday(day.year)
    return day in {
        easter + timedelta(days=int(offset))
        for offset in calendar.get("easter_offsets") or []
    }


def _calendar_for_region(definitions: Mapping[str, Any], region: str) -> dict[str, Any]:
    calendars = definitions.get("regions")
    regional = calendars.get(region.strip().casefold()) if isinstance(calendars, Mapping) else None
    default = definitions.get("default")
    base = dict(default) if isinstance(default, Mapping) else {}
    if not isinstance(regional, Mapping):
        return base
    inherited = regional.get("inherits")
    if inherited and inherited != "default":
        inherited_value = definitions.get(str(inherited))
        if isinstance(inherited_value, Mapping):
            base = dict(inherited_value)
    for key, value in regional.items():
        if key == "inherits":
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            base[key] = list(base.get(key) or []) + list(value)
        else:
            base[key] = value
    return base


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


def _normalize_rule_gop(gop: str) -> str:
    cleaned = _normalize_display_gop(gop)
    return cleaned[:5] if re.fullmatch(r"\d{5}[A-Z0-9*]*", cleaned) else cleaned


def _normalize_display_gop(gop: str) -> str:
    cleaned = str(gop).strip().upper().replace(" ", "")
    if cleaned.isdigit() and len(cleaned) == 4:
        cleaned = cleaned.zfill(5)
    return cleaned


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_text.lower()


def _diagnoses_from_evidence(evidence: Sequence[Mapping[str, Any]]) -> list[str]:
    diagnoses: list[str] = []
    pattern = re.compile(r"\b([A-Z]\d{2}(?:\.\d{1,2})?)\b")
    for item in evidence:
        metadata = item.get("metadata")
        if isinstance(metadata, Mapping):
            for key in ("icd10", "icd", "diagnosis"):
                value = metadata.get(key)
                if isinstance(value, str) and pattern.fullmatch(value.strip().upper()):
                    diagnoses.append(value.strip().upper())
            values = metadata.get("diagnoses")
            if isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
                for value in values:
                    if isinstance(value, str) and pattern.fullmatch(value.strip().upper()):
                        diagnoses.append(value.strip().upper())
        for key in ("value", "label", "text"):
            value = item.get(key)
            if isinstance(value, str):
                diagnoses.extend(match.group(1).upper() for match in pattern.finditer(value.upper()))
    return list(dict.fromkeys(diagnoses))


def _patient_age_from_evidence(evidence: Sequence[Mapping[str, Any]]) -> int | None:
    for item in evidence:
        metadata = item.get("metadata")
        if isinstance(metadata, Mapping):
            for key in ("patient_age", "age"):
                value = metadata.get(key)
                try:
                    age = int(str(value))
                except (TypeError, ValueError):
                    continue
                if 0 <= age <= 130:
                    return age
        for value in (item.get("text"), item.get("label")):
            if not isinstance(value, str):
                continue
            folded = _fold(value)
            patterns = (
                r"\balter\s*0?(\d{1,3})\s*(?:j|y|a|jahre?)\b",
                r"\b0?(\d{1,3})\s*(?:j|y)\b",
                r"\((\d{1,3})a\)",
            )
            for pattern in patterns:
                match = re.search(pattern, folded)
                if match:
                    age = int(match.group(1))
                    if 0 <= age <= 130:
                        return age
    return None


def _definition_applies(
    definition: CandidateRuleDefinition | TemporalRuleDefinition | EventSequenceRuleDefinition | DerivedRuleDefinition,
    quarter: str | None,
    region: str,
) -> bool:
    return definition_is_applicable(
        definition.valid_from,
        definition.valid_to,
        definition.regions,
        quarter,
        region,
    )


def _facts(
    gops: Sequence[str] | set[str] | frozenset[str],
    evidence: Sequence[Mapping[str, Any]],
    service_date: str | None,
    service_time: str | None,
    region: str,
    quarter: str | None,
) -> _RuleFacts:
    evidence_items = tuple(evidence)
    effective_date = service_date or _first_evidence_value(evidence_items, "service_date")
    effective_time = service_time or _first_evidence_value(evidence_items, "service_time")
    effective_quarter = quarter or _quarter_from_date(effective_date)
    text = _fold(
        " ".join(
            str(item.get(key) or "")
            for item in evidence_items
            for key in ("label", "text", "value")
        )
    )
    metadata_values: dict[str, list[Any]] = {}
    for item in evidence_items:
        metadata = item.get("metadata")
        if isinstance(metadata, Mapping):
            for key, value in metadata.items():
                metadata_values.setdefault(str(key), []).append(value)
    return _RuleFacts(
        gops=frozenset(_normalize_rule_gop(gop) for gop in gops),
        evidence=evidence_items,
        service_date=effective_date,
        service_time=effective_time,
        region=region,
        quarter=effective_quarter,
        text=text,
        diagnoses=tuple(_diagnoses_from_evidence(evidence_items)),
        patient_age=_patient_age_from_evidence(evidence_items),
        metadata={key: tuple(values) for key, values in metadata_values.items()},
    )


def _matches_condition(condition: Mapping[str, Any], facts: _RuleFacts) -> bool:
    results: list[bool] = []
    for operator, operand in condition.items():
        if operator == "all":
            results.append(all(_matches_condition(item, facts) for item in _condition_list(operand)))
        elif operator == "any":
            results.append(any(_matches_condition(item, facts) for item in _condition_list(operand)))
        elif operator == "not":
            results.append(not _matches_condition(_condition_object(operand), facts))
        elif operator == "always":
            results.append(bool(operand))
        elif operator == "gop_present":
            results.append(all(_normalize_rule_gop(value) in facts.gops for value in _string_values(operand)))
        elif operator == "gop_any":
            results.append(any(_normalize_rule_gop(value) in facts.gops for value in _string_values(operand)))
        elif operator == "gop_absent":
            results.append(all(_normalize_rule_gop(value) not in facts.gops for value in _string_values(operand)))
        elif operator in {"evidence_kind", "evidence_kind_any"}:
            expected = set(_string_values(operand))
            results.append(any(str(item.get("kind") or "") in expected for item in facts.evidence))
        elif operator == "evidence_kind_prefix":
            prefixes = tuple(_string_values(operand))
            results.append(any(str(item.get("kind") or "").startswith(prefixes) for item in facts.evidence))
        elif operator == "icd_any":
            expected = {value.upper() for value in _string_values(operand)}
            results.append(any(code.upper() in expected for code in facts.diagnoses))
        elif operator == "icd_prefix_any":
            prefixes = tuple(value.upper() for value in _string_values(operand))
            results.append(any(code.upper().startswith(prefixes) for code in facts.diagnoses))
        elif operator == "text_any":
            results.append(any(_fold(value) in facts.text for value in _string_values(operand)))
        elif operator == "text_all":
            results.append(all(_fold(value) in facts.text for value in _string_values(operand)))
        elif operator == "text_none":
            results.append(all(_fold(value) not in facts.text for value in _string_values(operand)))
        elif operator == "age":
            results.append(_matches_age(operand, facts.patient_age))
        elif operator == "special_day":
            day = _parse_date(facts.service_date)
            results.append(day is not None and is_special_calendar_day(day, facts.region) is bool(operand))
        elif operator == "weekday_any":
            day = _parse_date(facts.service_date)
            weekdays = {int(value) for value in _value_list(operand)}
            results.append(day is not None and day.weekday() in weekdays)
        elif operator == "time_window":
            results.append(_matches_time_window(operand, facts.service_time))
        elif operator == "region_any":
            regions = {value.casefold() for value in _string_values(operand)}
            results.append(facts.region.casefold() in regions or "*" in regions)
        elif operator == "quarter_between":
            results.append(_matches_quarter_range(operand, facts.quarter))
        elif operator == "metadata":
            results.append(_matches_metadata(operand, facts.metadata))
        else:
            raise ValueError(f"Unbekannter Regeloperator: {operator}")
    return bool(results) and all(results)


def _supporting_evidence(
    rule: DerivedRuleDefinition,
    evidence: Sequence[Mapping[str, Any]],
    gops: set[str],
    region: str,
    quarter: str | None,
) -> list[Mapping[str, Any]]:
    if not rule.supporting_evidence:
        return list(evidence)
    matches = [
        item
        for item in evidence
        if _matches_condition(rule.supporting_evidence, _facts(gops, (item,), None, None, region, quarter))
    ]
    return matches or list(evidence)


def _matches_age(operand: Any, age: int | None) -> bool:
    if not isinstance(operand, Mapping) or age is None:
        return False
    minimum = operand.get("min")
    maximum = operand.get("max")
    return (minimum is None or age >= int(minimum)) and (maximum is None or age <= int(maximum))


def _matches_time_window(operand: Any, service_time: str | None) -> bool:
    if not isinstance(operand, Mapping):
        return False
    clock = _parse_time(service_time)
    start = _parse_time(str(operand.get("start") or ""))
    end = _parse_time(str(operand.get("end") or ""))
    if clock is None or start is None or end is None:
        return False
    inside = start <= clock < end if start < end else clock >= start or clock < end
    return inside is bool(operand.get("inside", True))


def _matches_quarter_range(operand: Any, quarter: str | None) -> bool:
    if not isinstance(operand, Mapping):
        return False
    current = _quarter_index(quarter)
    lower = _quarter_index(str(operand.get("from") or "")) if operand.get("from") else None
    upper = _quarter_index(str(operand.get("to") or "")) if operand.get("to") else None
    return current is not None and (lower is None or current >= lower) and (upper is None or current <= upper)


def _matches_metadata(operand: Any, metadata: Mapping[str, tuple[Any, ...]]) -> bool:
    if not isinstance(operand, Mapping):
        return False
    key = str(operand.get("key") or "")
    values = metadata.get(key, ())
    if "present" in operand and bool(values) is not bool(operand.get("present")):
        return False
    if "equals" in operand:
        expected = str(operand.get("equals")).casefold()
        return any(str(value).casefold() == expected for value in values)
    if "in" in operand:
        expected = {value.casefold() for value in _string_values(operand.get("in"))}
        return any(str(value).casefold() in expected for value in values)
    return bool(values) if "present" not in operand else True


def _first_evidence_value(evidence: Sequence[Mapping[str, Any]], key: str) -> str | None:
    return next((str(item.get(key)) for item in evidence if item.get(key)), None)


def _condition_list(value: Any) -> list[Mapping[str, Any]]:
    return [_condition_object(item) for item in _value_list(value)]


def _condition_object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Eine verschachtelte Regelbedingung muss ein Objekt sein.")
    return value


def _string_values(value: Any) -> list[str]:
    return [str(item) for item in _value_list(value)]


def _value_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) else [value]


def _quarter_from_date(value: str | None) -> str | None:
    day = _parse_date(value)
    return f"{day.year}/Q{((day.month - 1) // 3) + 1}" if day else None


def _quarter_index(value: str | None) -> int | None:
    match = re.fullmatch(r"(\d{4})/Q([1-4])", str(value or "").strip().upper())
    return int(match.group(1)) * 4 + int(match.group(2)) - 1 if match else None


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
    return bool(
        re.search(
            r"\b(einmal|höchstens|maximal|je sitzung|je behandlungstag|am behandlungstag|"
            r"je behandlungsfall|im krankheitsfall|im quartal|nicht mehrfach)\b",
            rule_text,
        )
    )


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
