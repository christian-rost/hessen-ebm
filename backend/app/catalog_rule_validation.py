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
from .config import get_settings
from .site_definitions import load_site_definition_set
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
    # Konzept 3.4: Eine Luecke im obligaten Leistungsinhalt ist etwas anderes als
    # ein Ausschluss. Sie sagt nicht "nicht abrechenbar", sondern "so wie es
    # dokumentiert ist, noch nicht". Damit die Pipeline das unterscheiden kann,
    # nennt das Urteil die Klausel und die fehlenden Elemente beim Namen.
    clause_type: str | None = None
    missing_content: tuple[str, ...] = ()


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
        id(item): {"violations": [], "advisories": [], "content_gaps": []} for item in items
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
                for element in verdict.missing_content:
                    if element not in verdicts[id(item)]["content_gaps"]:
                        verdicts[id(item)]["content_gaps"].append(element)
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
                "content_gaps": verdicts[id(item)]["content_gaps"],
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
            # Keine erkannte Diagnose heisst nicht, dass die Bedingung verletzt ist.
            # Die Extraktion konnte sie nur nicht belegen; das ist ein Pruefhinweis.
            return ClauseVerdict(
                ADVISORY,
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
            if item.temporal_rule_id:
                # Die Zeitregel des Regelwerks hat Datum, Uhrzeit, Wochentag und
                # Feiertag gemeinsam bewertet und genau diese Variante gewaehlt.
                # Die kompilierte Klausel bildet davon oft nur das Zeitfenster ab,
                # nicht die Alternative "oder an Sonn- und Feiertagen". Sie darf
                # die vollstaendigere Entscheidung deshalb nicht ueberstimmen.
                return ClauseVerdict(
                    ADVISORY,
                    "Die Katalogbedingung nennt ein Zeitfenster, das die Leistungsuhrzeit nicht "
                    f"abdeckt; die Variante wurde über die Zeitregel des Regelwerks bestimmt: {source_text}",
                )
            return ClauseVerdict(
                VIOLATION, f"Leistungsuhrzeit liegt außerhalb der Katalogbedingung: {source_text}"
            )
        return None

    if clause_type == "requires_authorization":
        agreement = str(parameters.get("agreement") or "").strip()
        declared = {value.casefold() for value in _declared_authorizations()}
        if agreement and agreement.casefold() in declared:
            return None
        # Ob die Betriebsstaette die Genehmigung besitzt, steht nicht in der Akte.
        # Ohne ausdrueckliche Erklaerung wird die Position deshalb nicht automatisch
        # abgerechnet, sondern vorgelegt. Das ist die sichere Vorgabe: eine vorhandene
        # Genehmigung kostet eine Bestaetigung, eine fehlende sonst eine Falschabrechnung.
        return ClauseVerdict(
            VIOLATION,
            f"Die Position setzt eine Genehmigung nach der {agreement or 'genannten Vereinbarung'} voraus; "
            "für die Betriebsstätte ist keine solche Genehmigung erklärt.",
        )

    if clause_type == "required_service_content":
        required = [str(value) for value in _values(parameters.get("elements"))]
        missing = _uncovered_content(required, item.covered_service_content)
        if not missing:
            return None
        note = (
            "Obligater Leistungsinhalt ist nicht vollständig belegt: "
            + "; ".join(element[:80] for element in missing)
        )
        # Vorerst nur melden. Ob eine Luecke die Position blockiert, entscheidet
        # die Politik, damit sich das Verhalten ohne Codeaenderung scharf schalten laesst.
        blocking = bool((clause_policy or {}).get("required_service_content_blocks"))
        return ClauseVerdict(
            VIOLATION if blocking else ADVISORY,
            note,
            clause_type="required_service_content",
            missing_content=tuple(missing),
        )

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


def _uncovered_content(required: Sequence[str], covered: Sequence[str]) -> list[str]:
    """Pflichtelemente ohne Beleg.

    Das Modell gibt die Elemente woertlich zurueck, kann sie aber kuerzen oder
    umstellen. Verglichen wird deshalb ueber die Wortmenge.

    Zwei Richtungen zaehlen, und das ist der Kern. Frueher wurde nur gefragt, wie
    viel der Anforderung das Zitat abdeckt. Ein kurzes, korrektes Zitat konnte
    eine lange Anforderung damit nie erfuellen: An einem Produktionsentwurf lag
    der belegte "Persoenliche Arzt-Patienten-Kontakt im organisierten
    Not(-fall)dienst" bei 0,58 gegenueber einer Anforderung, die den Satz noch um
    die Aufzaehlung der Leistungserbringer verlaengert - und wurde als fehlend
    gemeldet, obwohl jedes seiner Woerter in der Anforderung stand.

    Ein Zitat, das fast vollstaendig in der Anforderung aufgeht, ist ein Beleg
    fuer sie. Damit daraus kein Freibrief wird, muss die Schnittmenge zugleich
    tragfaehig sein - zwei zufaellig geteilte Woerter belegen nichts.
    """
    normalized_covered = [_content_words(value) for value in covered if str(value).strip()]
    missing: list[str] = []
    for element in required:
        words = _content_words(element)
        if not words:
            continue
        if any(
            _covers(words, candidate) for candidate in normalized_covered if candidate
        ):
            continue
        missing.append(element)
    return missing


# Anteil der Anforderung, den ein Beleg abdecken muss, wenn er fuer sich steht.
CONTENT_COVERAGE_RATIO = 0.6
# Anteil des Belegs, der in der Anforderung aufgehen muss, damit er als Zitat
# aus ihr gilt, und die Mindestzahl gemeinsamer Woerter dafuer.
CONTENT_QUOTE_RATIO = 0.8
CONTENT_QUOTE_MIN_WORDS = 3


def _covers(required_words: set[str], covered_words: set[str]) -> bool:
    shared = required_words & covered_words
    if not shared:
        return False
    if len(shared) / len(required_words) >= CONTENT_COVERAGE_RATIO:
        return True
    return (
        len(shared) / len(covered_words) >= CONTENT_QUOTE_RATIO
        and len(shared) >= CONTENT_QUOTE_MIN_WORDS
    )


def _content_words(value: str) -> set[str]:
    return {word for word in re.findall(r"[a-zA-ZäöüÄÖÜß]{4,}", str(value).casefold())}


def _declared_authorizations() -> tuple[str, ...]:
    """Genehmigungen, die die Betriebsstaette erklaert hat."""
    return load_site_definition_set(get_settings().site_definitions_path).authorizations
