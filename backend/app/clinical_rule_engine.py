from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from .models import SelectionEntry


@dataclass(frozen=True)
class MatchContext:
    raw: str
    folded: str
    compact: str
    key: str
    segment_type: str | None = None
    segment_flags: frozenset[str] = frozenset()
    datetimes: dict[str, tuple[str | None, str | None]] | None = None
    selection_entries: tuple[SelectionEntry, ...] = ()

    def source(self, name: str) -> str:
        if name == "raw":
            return self.raw
        if name == "folded":
            return self.folded
        if name == "compact":
            return self.compact
        if name == "key":
            return self.key
        if name == "upper":
            return self.folded.upper()
        raise ValueError(f"Unbekannte Textquelle {name!r}.")


def normalize_text(
    text: str,
    *,
    segment_type: str | None = None,
    segment_flags: set[str] | frozenset[str] = frozenset(),
    datetimes: dict[str, tuple[str | None, str | None]] | None = None,
    selection_entries: list[SelectionEntry] | tuple[SelectionEntry, ...] = (),
) -> MatchContext:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    compact = re.sub(r"\s+", "", folded)
    key = re.sub(r"[^a-z0-9]+", "", compact)
    return MatchContext(
        raw=text,
        folded=folded,
        compact=compact,
        key=key,
        segment_type=segment_type,
        segment_flags=frozenset(segment_flags),
        datetimes=datetimes,
        selection_entries=tuple(selection_entries),
    )


def condition_matches(condition: dict[str, Any], context: MatchContext) -> bool:
    if "all" in condition:
        return all(condition_matches(item, context) for item in _conditions(condition["all"]))
    if "any" in condition:
        return any(condition_matches(item, context) for item in _conditions(condition["any"]))
    if "not" in condition:
        nested = condition["not"]
        return isinstance(nested, dict) and not condition_matches(nested, context)
    if "always" in condition:
        return bool(condition["always"])
    if "segment_any" in condition:
        return context.segment_type in _strings(condition["segment_any"])
    if "segment_flag_any" in condition:
        return bool(context.segment_flags.intersection(_strings(condition["segment_flag_any"])))
    if "segment_flag_all" in condition:
        return set(_strings(condition["segment_flag_all"])).issubset(context.segment_flags)
    if "datetime_present" in condition:
        roles = _strings(condition["datetime_present"])
        return any(context.datetimes and context.datetimes.get(role, (None, None))[0] for role in roles)
    if "selection_list_present" in condition:
        return bool(context.selection_entries) is bool(condition["selection_list_present"])
    if "selection_state_any" in condition:
        states = set(_strings(condition["selection_state_any"]))
        return any(entry.state in states for entry in context.selection_entries)
    if "selection_code_any" in condition:
        states, values = _selection_values(condition["selection_code_any"])
        return any(
            entry.state in states and entry.code.casefold() in values
            for entry in context.selection_entries
        )
    if "text_any" in condition:
        source, values = _text_values(condition["text_any"])
        text = context.source(source)
        return any(value.casefold() in text.casefold() for value in values)
    if "text_all" in condition:
        source, values = _text_values(condition["text_all"])
        text = context.source(source)
        return all(value.casefold() in text.casefold() for value in values)
    if "text_none" in condition:
        source, values = _text_values(condition["text_none"])
        text = context.source(source)
        return not any(value.casefold() in text.casefold() for value in values)
    if "regex_any" in condition:
        source, patterns = _text_values(condition["regex_any"], default_source="folded")
        return any(re.search(pattern, context.source(source), re.IGNORECASE) for pattern in patterns)
    if "regex_all" in condition:
        source, patterns = _text_values(condition["regex_all"], default_source="folded")
        return all(re.search(pattern, context.source(source), re.IGNORECASE) for pattern in patterns)
    if "internal_code_any" in condition:
        return any(_has_internal_code(context.folded, code) for code in _strings(condition["internal_code_any"]))
    raise ValueError(f"Nicht unterstützter klinischer Bedingungsoperator: {sorted(condition)}")


def matching_selection_entries(
    condition: dict[str, Any],
    context: MatchContext,
) -> tuple[SelectionEntry, ...]:
    matches: dict[str, SelectionEntry] = {}
    if "selection_code_any" in condition and condition_matches(condition, context):
        states, values = _selection_values(condition["selection_code_any"])
        for entry in context.selection_entries:
            if entry.state in states and entry.code.casefold() in values:
                matches[entry.code.casefold()] = entry
    for operator in ("all", "any"):
        if operator not in condition:
            continue
        for nested in _conditions(condition[operator]):
            if condition_matches(nested, context):
                for entry in matching_selection_entries(nested, context):
                    matches[entry.code.casefold()] = entry
    return tuple(matches.values())


def capture_value(capture: dict[str, Any], context: MatchContext) -> str | None:
    source = context.source(str(capture.get("source") or "folded"))
    pattern = str(capture.get("regex") or "")
    if not pattern:
        return None
    match = re.search(pattern, source, re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    group = int(capture.get("group") or 1)
    value = match.group(group)
    return value.strip() if value else None


def render_value(value: Any, variables: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(_DefaultFormatMap(variables))
    if isinstance(value, list):
        return [render_value(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: render_value(item, variables) for key, item in value.items()}
    return value


class _DefaultFormatMap(dict[str, Any]):
    def __missing__(self, key: str) -> str:
        return ""


def _has_internal_code(text: str, code: str) -> bool:
    pattern = rf"(?<![a-z0-9_])(?:\d+(?:[,.]\d+)?\s*x?\s*)?{re.escape(code.casefold())}(?![a-z0-9_])"
    return re.search(pattern, text.casefold()) is not None


def _text_values(value: Any, default_source: str = "key") -> tuple[str, list[str]]:
    if isinstance(value, dict):
        return str(value.get("source") or default_source), _strings(value.get("values"))
    return default_source, _strings(value)


def _selection_values(value: Any) -> tuple[set[str], set[str]]:
    if isinstance(value, dict):
        states = set(_strings(value.get("states") or ["checked"]))
        values = _strings(value.get("values"))
    else:
        states = {"checked"}
        values = _strings(value)
    return states, {item.casefold() for item in values}


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return []


def _conditions(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError("Verknüpfte klinische Bedingungen müssen JSON-Objekte sein.")
    return value
