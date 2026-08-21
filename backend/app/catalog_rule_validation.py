from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from datetime import time
from typing import Any, Mapping, Sequence

from .billing_rule_definitions import BillingRuleSet
from .billing_rule_store import (
    get_runtime_billing_rule_set,
    get_runtime_clinical_definition_set,
    load_compiled_catalog_rules,
)
from .clinical_definitions import ClinicalDefinitionSet
from .catalog import CatalogRepository, normalize_gop
from .ebm_rule_compiler import compile_catalog_quarter
from .models import BillingItem, Evidence


@dataclass(frozen=True)
class ClauseVerdict:
    """Ergebnis einer Katalogklausel.

    `violation` heisst: die Klausel war maschinell entscheidbar und ist verletzt;
    die GOP darf nicht automatisch abgerechnet werden. `advisory` heisst: die
    Klausel konnte nicht entschieden werden und bleibt als Pruefhinweis an der
    Position haengen.
    """

    severity: str
    note: str


VIOLATION = "violation"
ADVISORY = "advisory"


LONGITUDINAL_FREQUENCY_SCOPES = {
    "disease_case",
    "physician_case",
    "physician_group_case",
    "insured_case",
    "quarter",
    "calendar_week",
    "calendar_year",
}


def apply_catalog_rule_validation(
    items: list[BillingItem],
    evidence: list[Evidence],
    catalog: CatalogRepository,
    quarter: str,
    region: str,
    clinical_definitions: ClinicalDefinitionSet | None = None,
    rule_set: BillingRuleSet | None = None,
) -> dict[str, Any]:
    gop_bases = tuple(sorted({item.gop_base for item in items}))
    rows = load_compiled_catalog_rules(quarter, region, gop_bases)
    source = "supabase"
    if not rows and catalog.available and gop_bases:
        compiled = compile_catalog_quarter(
            catalog.db_path,
            quarter,
            region,
            include_regional=True,
            gop_bases=gop_bases,
        )
        rows = [
            {
                "rule_id": rule.rule_id,
                "definition_type": rule.definition_type,
                "gop": rule.gop,
                "gop_base": rule.gop_base,
                "title": rule.title,
                "source_type": rule.source_type,
                "coverage_status": rule.coverage_status,
                "scope": rule.scope,
                "definition": rule.definition_payload(),
            }
            for rule in compiled.rules
        ]
        source = "sqlite_compiler"

    facts = _evidence_facts(evidence, clinical_definitions or get_runtime_clinical_definition_set(quarter, region))
    by_gop: dict[str, list[dict[str, Any]]] = {gop: [] for gop in gop_bases}
    for row in rows:
        row_gop = str(row.get("gop_base") or "")
        if row_gop:
            by_gop.setdefault(row_gop, []).append(row)
            continue
        for gop in gop_bases:
            if _scope_applies(row.get("scope"), gop):
                by_gop.setdefault(gop, []).append(row)

    clause_policy = (rule_set or get_runtime_billing_rule_set(quarter, region)).clause_policy
    ignored = _ignored_clause_types(clause_policy)
    evaluated_clauses = 0
    review_notes = 0
    # Pro Position sammeln, ob eine entscheidbare Klausel verletzt ist. Das ist das
    # Abrechnungstor: nur Positionen ohne Verletzung duerfen automatisch entstehen.
    verdicts: dict[int, dict[str, list[str]]] = {
        id(item): {"violations": [], "advisories": []} for item in items
    }
    for item in items:
        for row in by_gop.get(item.gop_base, []):
            if not _definition_applicable(row, facts):
                continue
            definition = row.get("definition")
            clauses = definition.get("clauses", []) if isinstance(definition, Mapping) else []
            rule_notes: list[str] = []
            for clause in clauses:
                if not isinstance(clause, Mapping):
                    continue
                if str(clause.get("clause_type") or "") in ignored:
                    continue
                evaluated_clauses += 1
                scope = str(clause.get("scope") or "treatment_case")
                existing_counts = _counts_for_scope(items, item, scope)
                verdict = _evaluate_clause(clause, item, existing_counts, facts, clause_policy)
                if not verdict:
                    continue
                rule_notes.append(verdict.note)
                bucket = "violations" if verdict.severity == VIOLATION else "advisories"
                if verdict.note not in verdicts[id(item)][bucket]:
                    verdicts[id(item)][bucket].append(verdict.note)
            if rule_notes:
                new_notes = [note for note in rule_notes if note not in item.validation_notes]
                review_notes += len(new_notes)
                item.validation_notes.extend(new_notes)
                rule_id = str(row.get("rule_id") or "")
                if rule_id and rule_id not in item.rule_id:
                    item.rule_id = f"{item.rule_id}+{rule_id}"
        if item.validation_status == "catalog_missing":
            continue
        item.validation_status = "review" if verdicts[id(item)]["violations"] else item.validation_status

    return {
        "item_verdicts": [
            {
                "gop_original": item.gop_original,
                "service_event_id": item.service_event_id,
                "violations": verdicts[id(item)]["violations"],
                "advisories": verdicts[id(item)]["advisories"],
                "billable": not verdicts[id(item)]["violations"],
            }
            for item in items
        ],
        "source": source,
        "quarter": quarter,
        "region": region,
        "requested_gops": list(gop_bases),
        "matched_definitions": sum(len(values) for values in by_gop.values()),
        "evaluated_clauses": evaluated_clauses,
        "review_notes": review_notes,
    }


def _evaluate_clause(
    clause: Mapping[str, Any],
    item: BillingItem,
    existing_counts: Counter[str],
    facts: dict[str, Any],
    clause_policy: Mapping[str, Any] | None = None,
) -> ClauseVerdict | None:
    clause_type = str(clause.get("clause_type") or "")
    parameters = clause.get("parameters") if isinstance(clause.get("parameters"), Mapping) else {}
    source_text = str(clause.get("source_text") or "Katalogbedingung")
    scope = str(clause.get("scope") or "")

    if clause_type == "exclusion":
        excluded = {_gop_base(value) for value in _values(parameters.get("gops"))}
        conflicts = sorted((set(existing_counts) - {item.gop_base}).intersection(excluded))
        if conflicts:
            return ClauseVerdict(
                VIOLATION,
                f"Abrechnungsausschluss ({_scope_label(scope)}): nicht zusammen mit {', '.join(conflicts)}.",
            )
        return None

    if clause_type == "requires_gop":
        required = {_gop_base(value) for value in _values(parameters.get("gops"))}
        mode = str(parameters.get("mode") or "all")
        satisfied = bool(required.intersection(existing_counts)) if mode == "any" else required.issubset(existing_counts)
        if not satisfied:
            return ClauseVerdict(VIOLATION, f"GOP-Voraussetzung fehlt: {', '.join(sorted(required))}.")
        return None

    if clause_type == "frequency_limit":
        maximum = int(parameters.get("maximum") or 0)
        if maximum and existing_counts[item.gop_base] > maximum:
            return ClauseVerdict(
                VIOLATION,
                f"Häufigkeitsgrenze überschritten ({_scope_label(scope)}): maximal {maximum}, "
                f"vorhanden {existing_counts[item.gop_base]}.",
            )
        if scope in LONGITUDINAL_FREQUENCY_SCOPES:
            # Faelle ausserhalb dieses Dokuments sind hier nicht sichtbar.
            return ClauseVerdict(
                ADVISORY,
                f"Häufigkeitsgrenze ({_scope_label(scope)}) muss zusätzlich gegen die "
                "patientenbezogene Abrechnungshistorie geprüft werden.",
            )
        return None

    if clause_type == "requires_icd":
        if not facts["diagnoses"]:
            return ClauseVerdict(
                VIOLATION,
                "Katalogregel verlangt eine ICD-10-GM-Diagnose; keine gesicherte Diagnose wurde erkannt.",
            )
        return None

    if clause_type.startswith("requires_"):
        fact_name = clause_type[len("requires_") :]
        fact_definition = (facts.get("fact_definitions") or {}).get(fact_name)
        if fact_definition is not None:
            if (facts.get("flags") or {}).get(fact_name):
                return None
            note = str(fact_definition.get("missing_note") or "").strip()
            label = str(fact_definition.get("label") or fact_name)
            return ClauseVerdict(
                VIOLATION,
                note or f"Katalogregel verlangt {label}; dies ist nicht sicher belegt.",
            )

    if clause_type == "age_limit":
        age = facts["patient_age"]
        if age is None:
            return ClauseVerdict(ADVISORY, f"Altersbedingung muss geprüft werden: {source_text}")
        minimum = parameters.get("min_age")
        maximum = parameters.get("max_age")
        if minimum is not None and age < int(minimum):
            return ClauseVerdict(
                VIOLATION, f"Altersbedingung nicht erfüllt: Mindestalter {minimum}, erkanntes Alter {age}."
            )
        if maximum is not None and age > int(maximum):
            return ClauseVerdict(
                VIOLATION, f"Altersbedingung nicht erfüllt: Höchstalter {maximum}, erkanntes Alter {age}."
            )
        return None

    if clause_type == "time_window":
        if not item.service_time:
            return ClauseVerdict(
                ADVISORY, f"Zeitbedingung kann ohne Leistungsuhrzeit nicht geprüft werden: {source_text}"
            )
        if not _inside_time_window(item.service_time, parameters):
            return ClauseVerdict(
                VIOLATION, f"Leistungsuhrzeit liegt außerhalb der Katalogbedingung: {source_text}"
            )
        return None

    if clause_type in _advisory_clause_types(clause_policy):
        return ClauseVerdict(ADVISORY, f"Manuelle Prüfung der Katalogbedingung erforderlich: {source_text}")

    return None


def _counts_for_scope(items: list[BillingItem], current: BillingItem, scope: str) -> Counter[str]:
    if scope == "same_session":
        if current.service_session_id:
            relevant = [item for item in items if item.service_session_id == current.service_session_id]
        else:
            relevant = [
                item
                for item in items
                if item.service_date == current.service_date and item.service_time == current.service_time
            ]
    elif scope in {"treatment_day", "calendar_day"}:
        relevant = [item for item in items if item.service_date == current.service_date]
    elif scope == "quarter":
        relevant = [item for item in items if item.quarter == current.quarter]
    else:
        relevant = items
    return Counter(item.gop_base for item in relevant for _ in range(max(1, item.quantity)))


def _evidence_facts(evidence: Sequence[Evidence], definitions: ClinicalDefinitionSet) -> dict[str, Any]:
    text = " ".join(f"{item.label} {item.text} {item.value or ''}" for item in evidence)
    diagnoses = set(re.findall(r"\b[A-Z]\d{2}(?:\.\d{1,2})?\b", text.upper()))
    for item in evidence:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        for key in ("icd10", "icd", "diagnosis"):
            value = metadata.get(key)
            if isinstance(value, str) and re.fullmatch(r"[A-Z]\d{2}(?:\.\d{1,2})?", value.upper()):
                diagnoses.add(value.upper())
    fact_definitions = definitions.clause_facts
    return {
        "diagnoses": sorted(diagnoses),
        "patient_age": _patient_age(evidence),
        "fact_definitions": fact_definitions,
        "flags": {
            name: _clause_fact_holds(definition, evidence, text.casefold())
            for name, definition in fact_definitions.items()
        },
    }


def _clause_fact_holds(definition: Mapping[str, Any], evidence: Sequence[Evidence], folded_text: str) -> bool:
    """Prueft einen in `clinical_evidence_definitions.json` deklarierten Fakt.

    Ein Fakt gilt, wenn eine Evidenz das konfigurierte Metadatenflag traegt, ihre
    Evidenzart gelistet ist oder einer der konfigurierten Textmarker vorkommt.
    Fachbegriffe und Evidenzarten stehen damit ausschliesslich in den Definitionen.
    """
    flag = str(definition.get("metadata_flag") or "").strip()
    if flag:
        for item in evidence:
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            if bool(metadata.get(flag)):
                return True

    kinds = {str(value) for value in _values(definition.get("evidence_kinds"))}
    if kinds and any(item.kind in kinds for item in evidence):
        return True

    return any(str(value).casefold() in folded_text for value in _values(definition.get("text_any")))


def _patient_age(evidence: Sequence[Evidence]) -> int | None:
    for item in evidence:
        for key in ("patient_age", "age"):
            value = item.metadata.get(key) if isinstance(item.metadata, dict) else None
            try:
                age = int(value)
            except (TypeError, ValueError):
                continue
            if 0 <= age <= 130:
                return age
        match = re.search(r"\b(?:Alter\s*)?(\d{1,3})\s*(?:Jahre?|J\.?|a)\b", f"{item.label} {item.text}", re.IGNORECASE)
        if match and 0 <= int(match.group(1)) <= 130:
            return int(match.group(1))
    return None


def _inside_time_window(value: str, parameters: Mapping[str, Any]) -> bool:
    clock = _parse_time(value)
    start = _parse_time(str(parameters.get("start") or ""))
    end = _parse_time(str(parameters.get("end") or ""))
    if clock is None or start is None or end is None:
        return False
    return start <= clock < end if start < end else clock >= start or clock < end


def _parse_time(value: str) -> time | None:
    match = re.match(r"\s*(\d{1,2}):(\d{2})", value)
    if not match:
        return None
    try:
        return time(int(match.group(1)), int(match.group(2)))
    except ValueError:
        return None


def _values(value: Any) -> list[str]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [str(item) for item in value]
    return [str(value)] if value not in (None, "") else []


def _gop_base(value: str) -> str:
    return normalize_gop(value)[0]


def _scope_label(scope: str) -> str:
    return {
        "same_session": "dieselbe Sitzung",
        "treatment_day": "Behandlungstag",
        "calendar_day": "Kalendertag",
        "treatment_case": "Behandlungsfall",
        "disease_case": "Krankheitsfall",
        "physician_case": "Arztfall",
        "physician_group_case": "Arztgruppenfall",
        "insured_case": "Versichertenfall",
        "calendar_week": "Kalenderwoche",
        "calendar_year": "Kalenderjahr",
        "quarter": "Quartal",
    }.get(scope, scope or "Katalog")


def _scope_applies(scope: Any, gop: str) -> bool:
    if not isinstance(scope, Mapping):
        return False
    kind = scope.get("kind")
    if kind == "gop_range":
        return str(scope.get("start") or "") <= gop <= str(scope.get("end") or "")
    if kind == "global":
        return False
    return gop in {str(value) for value in _values(scope.get("affected_gops"))}


def _definition_applicable(row: Mapping[str, Any], facts: Mapping[str, Any]) -> bool:
    definition = row.get("definition")
    clauses = definition.get("clauses", []) if isinstance(definition, Mapping) else []
    age_clauses = [
        clause for clause in clauses
        if isinstance(clause, Mapping) and clause.get("clause_type") == "age_limit"
    ]
    age = facts.get("patient_age")
    if not age_clauses or age is None:
        return True
    for clause in age_clauses:
        parameters = clause.get("parameters") if isinstance(clause.get("parameters"), Mapping) else {}
        minimum = parameters.get("min_age")
        maximum = parameters.get("max_age")
        if minimum is not None and int(age) < int(minimum):
            return False
        if maximum is not None and int(age) > int(maximum):
            return False
    return True


def _advisory_clause_types(clause_policy: Mapping[str, Any] | None) -> set[str]:
    return {str(value) for value in (clause_policy or {}).get("advisory_clause_types") or []}


def _ignored_clause_types(clause_policy: Mapping[str, Any] | None) -> set[str]:
    return {str(value) for value in (clause_policy or {}).get("ignored_clause_types") or []}
