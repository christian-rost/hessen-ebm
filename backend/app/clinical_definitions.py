from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


CLINICAL_DEFINITIONS_PATH = Path(__file__).with_name("clinical_evidence_definitions.json")
SUPPORTED_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ClinicalDefinitionSet:
    schema_version: int
    definition_set_id: str
    version: str
    segment_types: dict[str, dict[str, Any]]
    segment_classifiers: tuple[dict[str, Any], ...]
    datetime_roles: dict[str, dict[str, Any]]
    state_tracks: tuple[dict[str, Any], ...]
    context_updates: tuple[dict[str, Any], ...]
    evidence_rules: tuple[dict[str, Any], ...]
    review_rules: tuple[dict[str, Any], ...]
    exclusion_rules: tuple[dict[str, Any], ...]
    selection_extraction: dict[str, Any]
    clause_facts: dict[str, dict[str, Any]]
    formats: dict[str, Any]


def clinical_definition_set_payload(definitions: ClinicalDefinitionSet) -> dict[str, Any]:
    return {
        "schema_version": definitions.schema_version,
        "definition_set_id": definitions.definition_set_id,
        "version": definitions.version,
        "formats": definitions.formats,
        "segment_types": definitions.segment_types,
        "segment_classifiers": list(definitions.segment_classifiers),
        "datetime_roles": definitions.datetime_roles,
        "state_tracks": list(definitions.state_tracks),
        "context_updates": list(definitions.context_updates),
        "evidence_rules": list(definitions.evidence_rules),
        "review_rules": list(definitions.review_rules),
        "exclusion_rules": list(definitions.exclusion_rules),
        "selection_extraction": definitions.selection_extraction,
        "clause_facts": definitions.clause_facts,
    }


def parse_clinical_definition_set(payload: dict[str, Any]) -> ClinicalDefinitionSet:
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Nicht unterstützte Evidenzschema-Version {schema_version}; erwartet wird {SUPPORTED_SCHEMA_VERSION}."
        )

    segment_types = _object_map(payload.get("segment_types"), "segment_types")
    classifiers = _object_list(payload.get("segment_classifiers"), "segment_classifiers")
    evidence_rules = _object_list(payload.get("evidence_rules"), "evidence_rules")
    review_rules = _object_list(payload.get("review_rules"), "review_rules")
    exclusion_rules = _object_list(payload.get("exclusion_rules"), "exclusion_rules")
    state_tracks = _object_list(payload.get("state_tracks"), "state_tracks")
    context_updates = _object_list(payload.get("context_updates"), "context_updates")

    for item in classifiers:
        segment_type = _required_text(item, "segment_type")
        if segment_type not in segment_types:
            raise ValueError(f"Klassifikationsregel verweist auf unbekannten Dokumenttyp {segment_type!r}.")
        _required_text(item, "rule_id")
        _condition(item.get("when"), item["rule_id"])

    _validate_rules(evidence_rules, "Evidenzregel")
    _validate_rules(review_rules, "Review-Regel")
    _validate_rules(exclusion_rules, "Ausschlussregel")
    _validate_unique_ids(classifiers + evidence_rules + review_rules + exclusion_rules)

    return ClinicalDefinitionSet(
        schema_version=schema_version,
        definition_set_id=_required_text(payload, "definition_set_id"),
        version=_required_text(payload, "version"),
        segment_types=segment_types,
        segment_classifiers=tuple(classifiers),
        datetime_roles=_object_map(payload.get("datetime_roles"), "datetime_roles"),
        state_tracks=tuple(state_tracks),
        context_updates=tuple(context_updates),
        evidence_rules=tuple(evidence_rules),
        review_rules=tuple(review_rules),
        exclusion_rules=tuple(exclusion_rules),
        selection_extraction=_optional_object(payload.get("selection_extraction"), "selection_extraction"),
        clause_facts=_object_map(payload.get("clause_facts") or {}, "clause_facts"),
        formats=dict(payload.get("formats") or {}),
    )


@lru_cache(maxsize=4)
def load_clinical_definition_set(path: str | Path | None = None) -> ClinicalDefinitionSet:
    source = Path(path) if path else CLINICAL_DEFINITIONS_PATH
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Die Evidenzdefinitionen müssen ein JSON-Objekt sein.")
    return parse_clinical_definition_set(payload)


def _validate_rules(items: list[dict[str, Any]], label: str) -> None:
    for item in items:
        rule_id = _required_text(item, "rule_id")
        _condition(item.get("when"), rule_id)
        if label == "Evidenzregel":
            _required_text(item, "kind")
            _required_text(item, "label")


def _condition(value: Any, rule_id: str) -> None:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"Regel {rule_id!r} enthält keine gültige Bedingung.")


def _validate_unique_ids(items: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for item in items:
        rule_id = _required_text(item, "rule_id")
        if rule_id in seen:
            duplicates.add(rule_id)
        seen.add(rule_id)
    if duplicates:
        raise ValueError(f"Doppelte klinische Regel-IDs: {', '.join(sorted(duplicates))}")


def _object_map(value: Any, field: str) -> dict[str, dict[str, Any]]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} muss ein JSON-Objekt sein.")
    result: dict[str, dict[str, Any]] = {}
    for key, item in value.items():
        if not isinstance(item, dict):
            raise ValueError(f"{field}.{key} muss ein JSON-Objekt sein.")
        result[str(key)] = dict(item)
    return result


def _object_list(value: Any, field: str) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} muss eine Liste von JSON-Objekten sein.")
    return [dict(item) for item in value]


def _optional_object(value: Any, field: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{field} muss ein JSON-Objekt sein.")
    return dict(value)


def _required_text(item: dict[str, Any], field: str) -> str:
    value = str(item.get(field) or "").strip()
    if not value:
        raise ValueError(f"Pflichtfeld {field!r} fehlt.")
    return value
