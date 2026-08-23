"""Mandantenspezifische Leistungskennungen.

Hausinterne Leistungscodes stammen aus dem KIS eines konkreten Standorts. Sie
sind weder aus dem EBM-Katalog noch aus klinischer Sprache ableitbar und
gehoeren deshalb nicht in das allgemeine Regelwerk. Diese Schicht haelt sie
getrennt, damit ein anderer Standort nur diese Datei austauscht.

Der Beitrag eines Standorts besteht aus drei Teilen:

`evidence_rules`      vollstaendige Evidenzregeln fuer eigene Leistungscodes
`marker_extensions`   zusaetzliche Marker fuer bestehende allgemeine Regeln
`candidate_rules`     Zuordnung eigener Evidenzarten zu GOP-Kandidaten
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any


SITE_DEFINITIONS_PATH = Path(__file__).with_name("site_service_codes.json")
SUPPORTED_SCHEMA_VERSION = 1
MARKER_FIELDS = ("text_any", "text_all", "text_none", "regex_any", "search_terms")


@dataclass(frozen=True)
class SiteDefinitionSet:
    schema_version: int = SUPPORTED_SCHEMA_VERSION
    site_id: str = ""
    version: str = ""
    evidence_rules: tuple[dict[str, Any], ...] = ()
    marker_extensions: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    candidate_rules: tuple[dict[str, Any], ...] = ()

    @property
    def empty(self) -> bool:
        return not (self.evidence_rules or self.marker_extensions or self.candidate_rules)


def parse_site_definition_set(payload: dict[str, Any]) -> SiteDefinitionSet:
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Nicht unterstützte Standortschema-Version {schema_version}; erwartet wird {SUPPORTED_SCHEMA_VERSION}."
        )
    extensions: dict[str, dict[str, list[str]]] = {}
    for rule_id, fields in (payload.get("marker_extensions") or {}).items():
        if not isinstance(fields, dict):
            raise ValueError(f"Markererweiterung {rule_id!r} muss ein JSON-Objekt sein.")
        cleaned = {
            str(key): [str(value) for value in values]
            for key, values in fields.items()
            if key in MARKER_FIELDS and isinstance(values, list)
        }
        if cleaned:
            extensions[str(rule_id)] = cleaned
    return SiteDefinitionSet(
        schema_version=schema_version,
        site_id=str(payload.get("site_id") or ""),
        version=str(payload.get("version") or ""),
        evidence_rules=tuple(payload.get("evidence_rules") or ()),
        marker_extensions=extensions,
        candidate_rules=tuple(payload.get("candidate_rules") or ()),
    )


@lru_cache(maxsize=4)
def load_site_definition_set(path: str | Path | None = None) -> SiteDefinitionSet:
    """Standortdefinitionen laden. Fehlt die Datei, bleibt die Schicht leer."""
    source = Path(path) if path else SITE_DEFINITIONS_PATH
    if not source.exists():
        return SiteDefinitionSet()
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Die Standortdefinitionen müssen ein JSON-Objekt sein.")
    return parse_site_definition_set(payload)


def apply_marker_extensions(
    rules: list[dict[str, Any]],
    extensions: dict[str, dict[str, list[str]]],
) -> list[dict[str, Any]]:
    """Standortmarker in bestehende Regeln einhaengen, ohne sie zu ueberschreiben."""
    if not extensions:
        return rules
    result: list[dict[str, Any]] = []
    for rule in rules:
        fields = extensions.get(str(rule.get("rule_id")))
        if not fields:
            result.append(rule)
            continue
        merged = json.loads(json.dumps(rule))
        for name, values in fields.items():
            if name == "search_terms":
                merged["search_terms"] = list(dict.fromkeys(list(merged.get("search_terms") or []) + values))
                continue
            _extend_condition(merged.get("when"), name, values)
        result.append(merged)
    return result


def _extend_condition(condition: Any, field_name: str, values: list[str], negated: bool = False) -> bool:
    """Alle passenden Bedingungsknoten um die Marker erweitern.

    Verzweigte Regeln fuehren dasselbe Feld mehrfach; ein Standortmarker muss in
    jedem Zweig gelten. Negierte Zweige bleiben ausgespart, weil ein zusaetzlicher
    Marker dort die Bedeutung umkehren wuerde.
    """
    extended = False
    if isinstance(condition, dict):
        if not negated and field_name in condition:
            target = condition[field_name]
            if isinstance(target, list):
                condition[field_name] = list(dict.fromkeys(target + values))
                extended = True
            elif isinstance(target, dict) and isinstance(target.get("values"), list):
                target["values"] = list(dict.fromkeys(target["values"] + values))
                extended = True
        for key, nested in condition.items():
            if key == field_name:
                continue
            if _extend_condition(nested, field_name, values, negated or key == "not"):
                extended = True
    elif isinstance(condition, list):
        for item in condition:
            if _extend_condition(item, field_name, values, negated):
                extended = True
    return extended
