from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


RULE_DEFINITIONS_PATH = Path(__file__).with_name("billing_rule_definitions.json")
SUPPORTED_SCHEMA_VERSION = 1
SUPPORTED_CONDITION_OPERATORS = {
    "all",
    "any",
    "not",
    "always",
    "gop_present",
    "gop_any",
    "gop_absent",
    "evidence_kind",
    "evidence_kind_any",
    "evidence_kind_prefix",
    "icd_any",
    "icd_prefix_any",
    "text_any",
    "text_all",
    "text_none",
    "age",
    "special_day",
    "weekday_any",
    "time_window",
    "region_any",
    "quarter_between",
    "metadata",
}


@dataclass(frozen=True)
class EvidenceRuleDefinition:
    rule_id: str
    evidence_kind: str
    gop: str
    title_hint: str
    confidence: str = "high"
    valid_from: str | None = None
    valid_to: str | None = None
    regions: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class TemporalOutcomeDefinition:
    rule_id: str
    gop: str
    when: dict[str, Any]
    note: str


@dataclass(frozen=True)
class TemporalRuleDefinition:
    rule_id: str
    name: str
    gops: tuple[str, ...]
    outcomes: tuple[TemporalOutcomeDefinition, ...]
    required_context: tuple[str, ...] = ()
    valid_from: str | None = None
    valid_to: str | None = None
    regions: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class EventSequenceRuleDefinition:
    rule_id: str
    name: str
    evidence_kinds: tuple[str, ...]
    initial_gop: str
    subsequent_gop: str
    session_gap_minutes: int = 90
    initial_role: str = "initial_contact"
    subsequent_role: str = "follow_up_contact"
    valid_from: str | None = None
    valid_to: str | None = None
    regions: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class CriterionDefinition:
    label: str
    when: dict[str, Any]


@dataclass(frozen=True)
class ExclusionDefinition:
    label: str
    when: dict[str, Any]
    action: str = "review"


@dataclass(frozen=True)
class DerivedRuleDefinition:
    rule_id: str
    gop: str
    title_hint: str
    evidence_kind: str
    requirements: tuple[dict[str, Any], ...]
    criteria: tuple[CriterionDefinition, ...] = ()
    exclusions: tuple[ExclusionDefinition, ...] = ()
    supporting_evidence: dict[str, Any] | None = None
    insert_after: str | None = None
    description: str = ""
    valid_from: str | None = None
    valid_to: str | None = None
    regions: tuple[str, ...] = ("*",)


@dataclass(frozen=True)
class BillingRuleSet:
    schema_version: int
    rule_set_id: str
    version: str
    evidence_rules: tuple[EvidenceRuleDefinition, ...]
    temporal_rules: tuple[TemporalRuleDefinition, ...]
    event_sequence_rules: tuple[EventSequenceRuleDefinition, ...]
    derived_rules: tuple[DerivedRuleDefinition, ...]


def parse_billing_rule_set(payload: dict[str, Any]) -> BillingRuleSet:
    schema_version = int(payload.get("schema_version") or 0)
    if schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(
            f"Nicht unterstützte Regelschema-Version {schema_version}; erwartet wird {SUPPORTED_SCHEMA_VERSION}."
        )

    evidence_rules = tuple(_parse_evidence_rule(item) for item in _objects(payload.get("evidence_rules")))
    temporal_rules = tuple(_parse_temporal_rule(item) for item in _objects(payload.get("temporal_rules")))
    event_sequence_rules = tuple(
        _parse_event_sequence_rule(item) for item in _objects(payload.get("event_sequence_rules"))
    )
    derived_rules = tuple(_parse_derived_rule(item) for item in _objects(payload.get("derived_rules")))
    rule_set = BillingRuleSet(
        schema_version=schema_version,
        rule_set_id=_required_text(payload, "rule_set_id"),
        version=_required_text(payload, "version"),
        evidence_rules=evidence_rules,
        temporal_rules=temporal_rules,
        event_sequence_rules=event_sequence_rules,
        derived_rules=derived_rules,
    )
    _validate_unique_rule_ids(rule_set)
    return rule_set


def billing_rule_set_payload(rule_set: BillingRuleSet) -> dict[str, Any]:
    # JSON-Rundlauf normalisiert Tupel und verschachtelte Dataclasses zu portablen Listen/Objekten.
    return json.loads(json.dumps(asdict(rule_set), ensure_ascii=False))


@lru_cache(maxsize=4)
def load_billing_rule_set(path: str | Path | None = None) -> BillingRuleSet:
    source = Path(path) if path else RULE_DEFINITIONS_PATH
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Das Regelwerk muss ein JSON-Objekt sein.")
    return parse_billing_rule_set(payload)


def definition_is_applicable(
    valid_from: str | None,
    valid_to: str | None,
    regions: tuple[str, ...],
    quarter: str | None,
    region: str,
) -> bool:
    normalized_regions = {value.strip().casefold() for value in regions}
    if "*" not in normalized_regions and region.strip().casefold() not in normalized_regions:
        return False
    if quarter is None:
        return True
    current = _quarter_index(quarter)
    if current is None:
        return False
    lower = _quarter_index(valid_from) if valid_from else None
    upper = _quarter_index(valid_to) if valid_to else None
    return (lower is None or current >= lower) and (upper is None or current <= upper)


def _parse_evidence_rule(item: dict[str, Any]) -> EvidenceRuleDefinition:
    return EvidenceRuleDefinition(
        rule_id=_required_text(item, "rule_id"),
        evidence_kind=_required_text(item, "evidence_kind"),
        gop=_required_gop(item, "gop"),
        title_hint=_required_text(item, "title_hint"),
        confidence=str(item.get("confidence") or "high"),
        valid_from=_optional_text(item.get("valid_from")),
        valid_to=_optional_text(item.get("valid_to")),
        regions=_regions(item),
    )


def _parse_temporal_rule(item: dict[str, Any]) -> TemporalRuleDefinition:
    outcomes = tuple(
        TemporalOutcomeDefinition(
            rule_id=_required_text(outcome, "rule_id"),
            gop=_required_gop(outcome, "gop"),
            when=_condition(outcome.get("when")),
            note=_required_text(outcome, "note"),
        )
        for outcome in _objects(item.get("outcomes"))
    )
    if not outcomes:
        raise ValueError(f"Zeitregel {_required_text(item, 'rule_id')} enthält keine Ergebnisse.")
    gops = tuple(_gop(value) for value in _values(item.get("gops")))
    if not gops:
        raise ValueError(f"Zeitregel {_required_text(item, 'rule_id')} enthält keine GOPs.")
    return TemporalRuleDefinition(
        rule_id=_required_text(item, "rule_id"),
        name=_required_text(item, "name"),
        gops=gops,
        outcomes=outcomes,
        required_context=tuple(str(value) for value in _values(item.get("required_context"))),
        valid_from=_optional_text(item.get("valid_from")),
        valid_to=_optional_text(item.get("valid_to")),
        regions=_regions(item),
    )


def _parse_event_sequence_rule(item: dict[str, Any]) -> EventSequenceRuleDefinition:
    evidence_kinds = tuple(str(value).strip() for value in _values(item.get("evidence_kinds")) if str(value).strip())
    if not evidence_kinds:
        raise ValueError(f"Ereignisregel {_required_text(item, 'rule_id')} enthält keine Evidenzarten.")
    session_gap_minutes = int(item.get("session_gap_minutes") or 90)
    if session_gap_minutes < 1:
        raise ValueError("Der Sitzungsabstand einer Ereignisregel muss mindestens eine Minute betragen.")
    return EventSequenceRuleDefinition(
        rule_id=_required_text(item, "rule_id"),
        name=_required_text(item, "name"),
        evidence_kinds=evidence_kinds,
        initial_gop=_required_gop(item, "initial_gop"),
        subsequent_gop=_required_gop(item, "subsequent_gop"),
        session_gap_minutes=session_gap_minutes,
        initial_role=str(item.get("initial_role") or "initial_contact"),
        subsequent_role=str(item.get("subsequent_role") or "follow_up_contact"),
        valid_from=_optional_text(item.get("valid_from")),
        valid_to=_optional_text(item.get("valid_to")),
        regions=_regions(item),
    )


def _parse_derived_rule(item: dict[str, Any]) -> DerivedRuleDefinition:
    return DerivedRuleDefinition(
        rule_id=_required_text(item, "rule_id"),
        gop=_required_gop(item, "gop"),
        title_hint=_required_text(item, "title_hint"),
        evidence_kind=_required_text(item, "evidence_kind"),
        requirements=tuple(_condition(value) for value in _objects(item.get("requirements"))),
        criteria=tuple(
            CriterionDefinition(
                label=_required_text(value, "label"),
                when=_condition(value.get("when")),
            )
            for value in _objects(item.get("criteria"))
        ),
        exclusions=tuple(
            ExclusionDefinition(
                label=_required_text(value, "label"),
                when=_condition(value.get("when")),
                action=str(value.get("action") or "review"),
            )
            for value in _objects(item.get("exclusions"))
        ),
        supporting_evidence=(
            _condition(item.get("supporting_evidence")) if item.get("supporting_evidence") is not None else None
        ),
        insert_after=_optional_gop(item.get("insert_after")),
        description=str(item.get("description") or ""),
        valid_from=_optional_text(item.get("valid_from")),
        valid_to=_optional_text(item.get("valid_to")),
        regions=_regions(item),
    )


def _validate_unique_rule_ids(rule_set: BillingRuleSet) -> None:
    ids = [rule.rule_id for rule in rule_set.evidence_rules]
    ids.extend(rule.rule_id for rule in rule_set.temporal_rules)
    ids.extend(outcome.rule_id for rule in rule_set.temporal_rules for outcome in rule.outcomes)
    ids.extend(rule.rule_id for rule in rule_set.event_sequence_rules)
    ids.extend(rule.rule_id for rule in rule_set.derived_rules)
    duplicates = sorted({rule_id for rule_id in ids if ids.count(rule_id) > 1})
    if duplicates:
        raise ValueError(f"Regel-IDs müssen eindeutig sein: {', '.join(duplicates)}")


def _required_text(item: dict[str, Any], key: str) -> str:
    value = str(item.get(key) or "").strip()
    if not value:
        raise ValueError(f"Pflichtfeld '{key}' fehlt im Regelwerk.")
    return value


def _required_gop(item: dict[str, Any], key: str) -> str:
    return _gop(_required_text(item, key))


def _optional_gop(value: Any) -> str | None:
    text = _optional_text(value)
    return _gop(text) if text else None


def _gop(value: Any) -> str:
    cleaned = str(value).strip().upper().replace(" ", "")
    if cleaned.isdigit() and len(cleaned) == 4:
        cleaned = cleaned.zfill(5)
    if not re.fullmatch(r"\d{5}[A-Z0-9*]*", cleaned):
        raise ValueError(f"Ungültige GOP im Regelwerk: {value!r}")
    return cleaned


def _condition(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError("Eine Regelbedingung muss ein nicht leeres JSON-Objekt sein.")
    condition = dict(value)
    for operator, operand in condition.items():
        if operator not in SUPPORTED_CONDITION_OPERATORS:
            raise ValueError(f"Unbekannter Regeloperator: {operator}")
        if operator in {"all", "any"}:
            for nested in _objects(operand):
                _condition(nested)
        elif operator == "not":
            _condition(operand)
    return condition


def _objects(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("Regellisten dürfen nur JSON-Objekte enthalten.")
    return list(value)


def _values(value: Any) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("Das Regelfeld muss eine Liste sein.")
    return value


def _regions(item: dict[str, Any]) -> tuple[str, ...]:
    values = item.get("regions", ["*"])
    regions = tuple(str(value).strip() for value in _values(values) if str(value).strip())
    return regions or ("*",)


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _quarter_index(value: str | None) -> int | None:
    match = re.fullmatch(r"(\d{4})/Q([1-4])", str(value or "").strip().upper())
    if not match:
        return None
    return int(match.group(1)) * 4 + int(match.group(2)) - 1
