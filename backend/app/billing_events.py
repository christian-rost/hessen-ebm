from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .billing_rule_definitions import (
    BillingRuleSet,
    EventSequenceRuleDefinition,
    definition_is_applicable,
)
from .billing_rule_store import get_runtime_billing_rule_set, get_runtime_clinical_definition_set
from .clinical_definitions import kinds_with_flags
from .models import BillingItem, Evidence


@dataclass
class BillingEvent:
    event_id: str
    kind: str
    evidence: list[Evidence]
    service_date: str | None
    service_time: str | None
    session_id: str | None = None
    episode_id: str | None = None
    primary_episode: bool = True
    temporal_role: str = "service_event"
    temporal_reason: str | None = None
    sequence_rule_id: str | None = None
    sequence_gop: str | None = None

    @property
    def evidence_ids(self) -> list[str]:
        return [item.evidence_id for item in self.evidence]

    @property
    def evidence_pages(self) -> list[int]:
        return sorted({item.page for item in self.evidence})


def build_billing_events(
    evidence: list[Evidence],
    quarter: str | None,
    region: str,
    rule_set: BillingRuleSet | None = None,
) -> list[BillingEvent]:
    definitions = rule_set or get_runtime_billing_rule_set(quarter, region)
    default_session_gap = _positive_setting(definitions.event_settings, "default_session_gap_minutes")
    episode_gap_days = _positive_setting(definitions.event_settings, "episode_gap_days")
    sequence_rules = [
        rule
        for rule in definitions.event_sequence_rules
        if definition_is_applicable(rule.valid_from, rule.valid_to, rule.regions, quarter, region)
    ]
    rule_by_kind = {
        kind: rule
        for rule in sequence_rules
        for kind in rule.evidence_kinds
    }
    flag_rules = [(frozenset(rule.evidence_flags), rule) for rule in sequence_rules if rule.evidence_flags]

    events: list[BillingEvent] = []
    by_kind: dict[str, list[Evidence]] = {}
    for item in evidence:
        by_kind.setdefault(item.kind, []).append(item)

    for kind, matches in by_kind.items():
        sequence_rule = rule_by_kind.get(kind)
        if sequence_rule is None and flag_rules:
            present = frozenset().union(*(evidence_flags(item) for item in matches)) if matches else frozenset()
            clinical = get_runtime_clinical_definition_set()
            sequence_rule = next(
                (
                    rule
                    for wanted, rule in flag_rules
                    if (present & wanted) or kind in kinds_with_flags(clinical, wanted)
                ),
                None,
            )
        gap = sequence_rule.session_gap_minutes if sequence_rule else default_session_gap
        for cluster in _cluster_evidence(matches, gap, definitions.event_settings):
            selected = _select_event_anchor(cluster, definitions.event_settings)
            events.append(
                BillingEvent(
                    event_id=_event_id(kind, cluster),
                    kind=kind,
                    evidence=cluster,
                    service_date=selected.service_date,
                    service_time=selected.service_time,
                )
            )

    _assign_episodes(events, definitions, episode_gap_days)
    _assign_sessions(events, default_session_gap)
    for rule in sequence_rules:
        _apply_sequence_rule(events, rule)
    return sorted(events, key=_event_sort_key)


def events_for_evidence_kind(events: list[BillingEvent], evidence_kind: str) -> list[BillingEvent]:
    return [event for event in events if event.kind == evidence_kind and event.primary_episode]


def events_for_evidence_ids(events: list[BillingEvent], evidence_ids: list[str]) -> list[BillingEvent]:
    requested = set(evidence_ids)
    return [event for event in events if requested.intersection(event.evidence_ids)]


def primary_episode_evidence(events: list[BillingEvent]) -> list[Evidence]:
    evidence_by_id = {
        item.evidence_id: item
        for event in events
        if event.primary_episode
        for item in event.evidence
    }
    return list(evidence_by_id.values())


def episode_selection_payload(events: list[BillingEvent]) -> dict[str, object]:
    episode_gap_days = _positive_setting(
        get_runtime_billing_rule_set().event_settings,
        "episode_gap_days",
    )
    by_episode: dict[str, list[BillingEvent]] = {}
    for event in events:
        by_episode.setdefault(event.episode_id or "episode-undated", []).append(event)
    episodes = []
    for episode_id, episode_events in sorted(by_episode.items()):
        dates = sorted({event.service_date for event in episode_events if event.service_date})
        pages = sorted({page for event in episode_events for page in event.evidence_pages})
        episodes.append(
            {
                "episode_id": episode_id,
                "primary": any(event.primary_episode for event in episode_events),
                "start_date": dates[0] if dates else None,
                "end_date": dates[-1] if dates else None,
                "event_count": len(episode_events),
                "evidence_pages": pages,
            }
        )
    return {"episode_gap_days": episode_gap_days, "episodes": episodes}


def finalize_billing_timeline(items: list[BillingItem]) -> None:
    original_order = {id(item): index for index, item in enumerate(items)}
    settings = get_runtime_billing_rule_set().event_settings
    role_priority = {
        str(role): int(priority)
        for role, priority in (settings.get("timeline_role_priority") or {}).items()
    }
    items.sort(
        key=lambda item: (
            item.service_date or "9999-12-31",
            role_priority.get(item.temporal_role, 1),
            item.service_time or "23:59",
            original_order[id(item)],
        )
    )
    for index, item in enumerate(items, start=1):
        item.line = index
        item.temporal_sequence = index


def _cluster_evidence(
    matches: list[Evidence],
    gap_minutes: int,
    event_settings: dict[str, object],
) -> list[list[Evidence]]:
    dated = sorted(matches, key=_evidence_sort_key)
    clusters: list[list[Evidence]] = []
    for item in dated:
        if not clusters or not _same_session(clusters[-1], item, gap_minutes, event_settings):
            clusters.append([item])
        else:
            clusters[-1].append(item)
    return clusters


def _same_session(
    cluster: list[Evidence],
    item: Evidence,
    gap_minutes: int,
    event_settings: dict[str, object],
) -> bool:
    anchor = _select_event_anchor(cluster, event_settings)
    force_separate = item.metadata.get("force_separate_event") or any(
        evidence.metadata.get("force_separate_event") for evidence in cluster
    )
    if force_separate and (
        anchor.service_date != item.service_date or anchor.service_time != item.service_time
    ):
        return False
    if not anchor.service_date or not item.service_date:
        return True
    if not anchor.service_time or not item.service_time:
        return anchor.service_date == item.service_date
    anchor_datetime = _parse_datetime(anchor.service_date, anchor.service_time)
    item_datetime = _parse_datetime(item.service_date, item.service_time)
    if not anchor_datetime or not item_datetime:
        return anchor.service_date == item.service_date
    return abs((item_datetime - anchor_datetime).total_seconds()) <= gap_minutes * 60


def evidence_flags(item: Evidence) -> frozenset[str]:
    """Metadatenmerkmale einer Evidenz, z. B. `emergency_contact`."""
    metadata = item.metadata if isinstance(item.metadata, dict) else {}
    return frozenset(str(key) for key, value in metadata.items() if value is True)


def event_matches_sequence(event: BillingEvent, rule: EventSequenceRuleDefinition) -> bool:
    """Gehoert das Ereignis zur Kontaktsequenz?

    Entweder ist seine Evidenzart ausdruecklich genannt, oder eine seiner
    Evidenzen traegt eines der geforderten Merkmale. Das Merkmal ist der
    generische Weg: eine neue Evidenzart, die denselben Sachverhalt bedeutet,
    wird damit erfasst, ohne dass die Regel geaendert werden muss.
    """
    if event.kind in rule.evidence_kinds:
        return True
    if not rule.evidence_flags:
        return False
    wanted = frozenset(rule.evidence_flags)
    if event.kind in kinds_with_flags(get_runtime_clinical_definition_set(), wanted):
        return True
    return any(evidence_flags(item) & wanted for item in event.evidence)


def _apply_sequence_rule(events: list[BillingEvent], rule: EventSequenceRuleDefinition) -> None:
    episode_ids = {event.episode_id for event in events if event_matches_sequence(event, rule)}
    for episode_id in episode_ids:
        matching = sorted(
            (
                event
                for event in events
                if event_matches_sequence(event, rule) and event.episode_id == episode_id
            ),
            key=_event_sort_key,
        )
        for index, event in enumerate(matching):
            initial = index == 0
            event.temporal_role = rule.initial_role if initial else rule.subsequent_role
            event.sequence_gop = rule.initial_gop if initial else rule.subsequent_gop
            event.sequence_rule_id = rule.rule_id
            if initial:
                event.temporal_reason = f"Erster chronologisch erkannter Kontakt im Behandlungsfall ({rule.name})."
            else:
                event.temporal_reason = (
                    f"Weiterer chronologisch erkannter Kontakt im Behandlungsfall ({rule.name}); "
                    f"vorheriger Kontakt am {_display_date(matching[index - 1].service_date)}."
                )


def _assign_episodes(
    events: list[BillingEvent],
    rule_set: BillingRuleSet,
    episode_gap_days: int,
) -> None:
    dated_events = sorted((event for event in events if event.service_date), key=_event_sort_key)
    episodes: list[list[BillingEvent]] = []
    for event in dated_events:
        if not episodes or _days_between(
            episodes[-1][-1].service_date,
            event.service_date,
            episode_gap_days,
        ) > episode_gap_days:
            episodes.append([event])
        else:
            episodes[-1].append(event)

    for event in (event for event in events if not event.service_date):
        if episodes:
            nearest = min(
                episodes,
                key=lambda episode: min(abs(event.evidence_pages[0] - page) for member in episode for page in member.evidence_pages),
            )
            nearest.append(event)
        else:
            episodes.append([event])

    # Frueher wurde nach "wie viele Ereignisse sind abrechenbar" sortiert. Mit dem
    # Wegfall der Allowlist ist das vor der Katalogpruefung nicht mehr bekannt;
    # massgeblich sind jetzt Umfang und Belegdichte des Behandlungsabschnitts.
    ranked = sorted(
        enumerate(episodes),
        key=lambda value: (
            len(value[1]),
            len({page for event in value[1] for page in event.evidence_pages}),
            -value[0],
        ),
        reverse=True,
    )
    primary_index = ranked[0][0] if ranked else 0
    for index, episode in enumerate(episodes):
        digest = hashlib.sha1("|".join(sorted(event.event_id for event in episode)).encode("utf-8")).hexdigest()[:12]
        episode_id = f"episode-{digest}"
        for event in episode:
            event.episode_id = episode_id
            event.primary_episode = index == primary_index


def _assign_sessions(events: list[BillingEvent], session_gap_minutes: int) -> None:
    sessions: list[list[BillingEvent]] = []
    for event in sorted(events, key=_event_sort_key):
        if not sessions or not _events_share_session(sessions[-1][-1], event, session_gap_minutes):
            sessions.append([event])
        else:
            sessions[-1].append(event)
    for session in sessions:
        digest = hashlib.sha1("|".join(sorted(event.event_id for event in session)).encode("utf-8")).hexdigest()[:12]
        session_id = f"session-{digest}"
        for event in session:
            event.session_id = session_id


def _events_share_session(
    previous: BillingEvent,
    current: BillingEvent,
    session_gap_minutes: int,
) -> bool:
    if not previous.service_date or not current.service_date:
        return False
    if not previous.service_time or not current.service_time:
        return previous.service_date == current.service_date
    previous_datetime = _parse_datetime(previous.service_date, previous.service_time)
    current_datetime = _parse_datetime(current.service_date, current.service_time)
    if not previous_datetime or not current_datetime:
        return previous.service_date == current.service_date
    return abs((current_datetime - previous_datetime).total_seconds()) <= session_gap_minutes * 60


def _select_event_anchor(matches: list[Evidence], event_settings: dict[str, object]) -> Evidence:
    dated = [item for item in matches if item.service_date]
    if not dated:
        return max(matches, key=lambda item: item.confidence)
    strategies = event_settings.get("anchor_strategies") or []
    for strategy in strategies:
        if not isinstance(strategy, dict):
            continue
        prefix = str(strategy.get("evidence_kind_prefix") or "")
        if prefix and not matches[0].kind.startswith(prefix):
            continue
        fields = [str(field) for field in strategy.get("sort_fields") or []]
        if fields:
            return min(dated, key=lambda item: _anchor_sort_key(item, fields))
    raise ValueError("Im aktiven Regelwerk fehlt eine passende Ereignisanker-Strategie.")


def _anchor_sort_key(item: Evidence, fields: list[str]) -> tuple[object, ...]:
    values: list[object] = []
    for field in fields:
        descending = field.endswith("_desc")
        attribute = field.removesuffix("_desc")
        value = getattr(item, attribute, None)
        if attribute == "confidence":
            number = float(value or 0)
            values.append(-number if descending else number)
        elif attribute == "page":
            number = int(value or 0)
            values.append(-number if descending else number)
        else:
            text = str(value) if value is not None else "9999-12-31"
            values.append(_invert_text(text) if descending else text)
    return tuple(values)


def _invert_text(value: str) -> tuple[int, ...]:
    return tuple(-ord(character) for character in value)


def _event_id(kind: str, evidence: list[Evidence]) -> str:
    digest = hashlib.sha1("|".join(sorted(item.evidence_id for item in evidence)).encode("utf-8")).hexdigest()[:12]
    return f"event-{digest}"


def _evidence_sort_key(item: Evidence) -> tuple[str, str, int, str]:
    return item.service_date or "9999-12-31", item.service_time or "23:59", item.page, item.evidence_id


def _event_sort_key(event: BillingEvent) -> tuple[str, str, int, str]:
    first_page = min(event.evidence_pages) if event.evidence_pages else 0
    return event.service_date or "9999-12-31", event.service_time or "23:59", first_page, event.event_id


def _parse_datetime(date_value: str, time_value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(f"{date_value}T{time_value}")
    except ValueError:
        return None


def _days_between(first: str | None, second: str | None, fallback_days: int) -> int:
    try:
        return abs((datetime.fromisoformat(second or "") - datetime.fromisoformat(first or "")).days)
    except ValueError:
        return fallback_days + 1


def _positive_setting(settings: dict[str, object], name: str) -> int:
    value = int(settings.get(name) or 0)
    if value <= 0:
        raise ValueError(f"Im aktiven Regelwerk fehlt die positive Ereigniseinstellung {name!r}.")
    return value


def _display_date(value: str | None) -> str:
    try:
        return datetime.fromisoformat(value or "").strftime("%d.%m.%Y")
    except ValueError:
        return "unbekannten Datum"
