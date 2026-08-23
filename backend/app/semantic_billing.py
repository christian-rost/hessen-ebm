from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Union

from .billing_rules import (
    BillingRuleContext,
    apply_temporal_gop_rule,
    billing_rule_guidance,
    candidate_gops_for_evidence_kind,
    non_binding_gops_for_evidence_kind,
    derive_additional_gops,
    evaluate_catalog_context_rules,
)
from .billing_events import (
    evidence_flags,
    BillingEvent,
    build_billing_events,
    episode_selection_payload,
    events_for_evidence_ids,
    finalize_billing_timeline,
    primary_episode_evidence,
)
from .billing_rule_definitions import BillingRuleSet, definition_is_applicable
from .billing_rule_store import get_runtime_billing_rule_set
from .catalog import CatalogRepository, canonical_gop, normalize_gop
from .catalog_rule_validation import apply_catalog_rule_validation
from .config import Settings
from .evidence_extraction import quarter_from_date
from .models import (
    BillingItem,
    CatalogEntry,
    DocumentationHint,
    Evidence,
    ExcludedEvidence,
    InvoiceSummary,
    ReviewCandidate,
)
from .rule_engine import (
    _resolve_quarter,
    append_derived_billing_items,
    reconcile_derived_item_anchors,
)


class SemanticBillingError(RuntimeError):
    pass


LlmClient = Callable[[list[dict[str, str]], Settings], Union[dict[str, Any], str]]


@dataclass(frozen=True)
class SemanticBillingResult:
    items: list[BillingItem]
    summary: InvoiceSummary
    review_candidates: list[ReviewCandidate]
    excluded_evidence: list[ExcludedEvidence]
    documentation_hints: list[DocumentationHint]
    context: dict[str, Any]


def generate_semantic_billing_items(
    evidence: list[Evidence],
    catalog: CatalogRepository,
    default_quarter: str | None,
    settings: Settings,
    region: str = "Hessen",
    llm_client: LlmClient | None = None,
) -> SemanticBillingResult:
    if not settings.enable_semantic_billing:
        raise SemanticBillingError("Die semantische Abrechnung ist deaktiviert.")
    if not settings.mistral_api_key and llm_client is None:
        raise SemanticBillingError("MISTRAL_API_KEY ist nicht konfiguriert.")

    quarter = _resolve_quarter(_quarter_from_evidence(evidence), default_quarter, catalog)
    events = build_billing_events(evidence, quarter, region)
    billing_evidence = primary_episode_evidence(events)
    candidates = _collect_catalog_candidates(billing_evidence, catalog, quarter, region)
    if not candidates:
        raise SemanticBillingError(f"Für das Quartal {quarter} wurden keine Katalogkandidaten gefunden.")

    messages = _build_messages(billing_evidence, candidates, quarter, region)
    rule_set = get_runtime_billing_rule_set(quarter, region)
    payload, attempts, agreement, passes = _derive_over_passes(
        messages, settings, llm_client, rule_set.semantic_policy
    )

    items, item_review = _billing_items_from_payload(
        payload, billing_evidence, events, candidates, catalog, quarter, region, agreement, passes
    )
    reconcile_derived_item_anchors(items, quarter, region)
    append_derived_billing_items(items, billing_evidence, catalog, quarter, region)
    catalog_rule_validation = [
        apply_catalog_rule_validation(
            [item for item in items if item.quarter == item_quarter],
            billing_evidence,
            catalog,
            item_quarter,
            region,
        )
        for item_quarter in sorted({item.quarter for item in items})
    ]
    items, blocked_review, content_gap_hints = _split_items_by_catalog_verdict(
        items, catalog_rule_validation
    )
    finalize_billing_timeline(items)
    review = item_review + blocked_review + _review_from_payload(payload, billing_evidence)
    excluded = _excluded_from_payload(payload, billing_evidence)
    hints = _documentation_hints_from_payload(
        payload, billing_evidence, candidates, catalog, quarter, region
    )
    hints += content_gap_hints
    review += _uncovered_service_days(events, items, hints)
    summary = InvoiceSummary(
        line_count=len(items),
        points_total=sum((item.points or 0) * item.quantity for item in items),
        amount_total_eur=round(sum((item.amount_eur or 0.0) * item.quantity for item in items), 2),
        human_review_required=True,
    )

    return SemanticBillingResult(
        items=items,
        summary=summary,
        review_candidates=review,
        excluded_evidence=excluded,
        documentation_hints=hints,
        context={
            "mode": "semantic_llm",
            "provider": "mistral",
            "model": settings.mistral_llm_model,
            "quarter": quarter,
            "region": region,
            "llm_attempts": attempts,
            "derivation_passes": passes,
            "pass_agreement": {f"{gop}@{date or ''}": count for (gop, date), count in sorted(agreement.items())},
            "catalog_candidate_count": len(candidates),
            "billing_event_count": len(events),
            "episode_selection": episode_selection_payload(events),
            "catalog_rule_validation": catalog_rule_validation,
        },
    )


def _derive_over_passes(
    messages: list[dict[str, str]],
    settings: Settings,
    llm_client: LlmClient | None,
    semantic_policy: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[tuple[str, str | None], int], int]:
    """Mehrfach ableiten und vereinigen.

    Gemessen an vier Laeufen desselben Falls mit derselben Fassung und
    temperature 0: kein Lauf enthielt alle Sollpositionen, jeder einzelne war
    frei von Falschvorschlaegen, und die Vereinigung traf genau die Sollmenge.
    Das Modell findet also jede richtige Position - nur nie alle auf einmal.

    Bei dieser Fehlerverteilung ist ein zweiter Durchgang die einzige Massnahme,
    die etwas bringt, ohne etwas zu riskieren: Was ein Lauf zusaetzlich findet,
    ist nach dieser Messung eher eine gefundene als eine erfundene Position - und
    das Katalogtor prueft ohnehin jede davon. Der Preis sind Modellaufrufe.

    Zurueck kommt neben der vereinigten Antwort, in wie vielen Durchgaengen jede
    Position auftrat. Eine Position, die nur einer von vier Durchgaengen kennt,
    ist schwaecher belegt als eine, die alle nennen - das faellt in die
    Konfidenz und damit in die Vorlage zur Pruefung.
    """
    passes = max(1, int(semantic_policy.get("derivation_passes") or 1))
    merged: dict[str, Any] = {"items": [], "documentation_hints": [], "review_candidates": [], "excluded_evidence": []}
    agreement: dict[tuple[str, str | None], int] = {}
    seen: dict[str, dict[tuple[str, str | None], dict[str, Any]]] = {key: {} for key in merged}
    attempts: list[dict[str, Any]] = []
    failures: list[str] = []

    for index in range(1, passes + 1):
        # Je Durchgang einmal zaehlen. Schlaegt dasselbe Modell eine Position
        # innerhalb einer Antwort zweimal vor, ist das keine Bestaetigung durch
        # einen zweiten Durchgang - sonst waere ein Doppelvorschlag so viel wert
        # wie eine unabhaengige Wiederholung.
        in_diesem_durchgang: set[tuple[str, str | None]] = set()
        try:
            payload, pass_attempts = _request_payload_with_retry(
                messages, settings, llm_client, semantic_policy
            )
        except SemanticBillingError as exc:
            # Ein gescheiterter Durchgang macht die anderen nicht wertlos.
            failures.append(str(exc))
            attempts.append({"pass": index, "status": "failed", "reason": str(exc)})
            continue
        for attempt in pass_attempts:
            attempts.append({**attempt, "pass": index})
        for key in merged:
            for raw in _as_list(payload.get(key)):
                if not isinstance(raw, dict):
                    continue
                # Ohne Datum unterscheidet erst die Belegangabe zwei Vorschlaege
                # derselben GOP. Fehlt beides, sind es tatsaechlich dieselben.
                anchor = _clean_optional_str(raw.get("service_date")) or ",".join(
                    sorted(str(value) for value in _as_list(raw.get("evidence_ids")))
                )
                identity = (
                    canonical_gop(str(raw.get("gop") or raw.get("not_billed_gop") or raw.get("evidence") or "")),
                    anchor or None,
                )
                if key == "items" and identity not in in_diesem_durchgang:
                    in_diesem_durchgang.add(identity)
                    agreement[identity] = agreement.get(identity, 0) + 1
                previous = seen[key].get(identity)
                if previous is None:
                    seen[key][identity] = raw
                    merged[key].append(raw)
                elif len(_as_list(raw.get("evidence_ids"))) > len(_as_list(previous.get("evidence_ids"))):
                    # Denselben Vorschlag mit der reicheren Belegangabe behalten.
                    merged[key][merged[key].index(previous)] = raw
                    seen[key][identity] = raw

    if not any(merged[key] for key in merged) and failures:
        raise SemanticBillingError(failures[0])
    return merged, attempts, agreement, passes


def _uncovered_service_days(
    events: list[BillingEvent],
    items: list[BillingItem],
    hints: list[DocumentationHint],
) -> list[ReviewCandidate]:
    """Behandlungstage ohne jede Position melden.

    Ein frueherer Versuch hat dasselbe je Evidenz geprueft und 38 Eintraege
    erzeugt - unbrauchbar, weil die meisten Belege zu Recht in einer Pauschale
    aufgehen. Der Behandlungstag ist die richtige Koernung: Er ist die Einheit,
    in der abgerechnet wird, es gibt wenige davon, und ein Tag mit dokumentierten
    Leistungsereignissen und null Positionen ist immer erklaerungsbeduerftig.

    Konzept 3.3 nennt die Timeline das zentrale Zwischenartefakt. Sie ist
    deterministisch aufgebaut; damit ist auch diese Gegenprobe deterministisch
    und haengt nicht daran, ob das Modell einen Tag uebersehen hat.
    """
    abgedeckt = {item.service_date for item in items if item.service_date}
    abgedeckt |= {hint.service_date for hint in hints if hint.service_date}

    nach_tag: dict[str, list[BillingEvent]] = {}
    for event in events:
        if not event.primary_episode or not event.service_date:
            continue
        nach_tag.setdefault(event.service_date, []).append(event)

    offen: list[ReviewCandidate] = []
    for tag in sorted(set(nach_tag) - abgedeckt):
        tages_events = nach_tag[tag]
        evidence_ids = sorted({eid for event in tages_events for eid in event.evidence_ids})
        seiten = sorted({item.page for event in tages_events for item in event.evidence})
        offen.append(
            ReviewCandidate(
                evidence=f"Behandlungstag {tag} ohne Position",
                evidence_pages=seiten,
                evidence_ids=evidence_ids,
                possible_gops=[],
                reason=(
                    f"An diesem Tag sind {len(tages_events)} Leistungsereignisse dokumentiert, "
                    "der Entwurf enthält dafür aber keine einzige Position und auch keinen "
                    "Dokumentationshinweis. Bitte prüfen, ob eine Leistung übersehen wurde."
                ),
            )
        )
    return offen


def _documentation_hints_from_payload(
    payload: dict[str, Any],
    evidence: list[Evidence],
    candidates: list[dict[str, Any]],
    catalog: CatalogRepository,
    quarter: str,
    region: str,
) -> list[DocumentationHint]:
    """Konzept 3.4: erbracht, aber nicht vollstaendig dokumentiert.

    Es gilt dasselbe Tor wie fuer Positionen - nur GOPs aus dem Kandidatenpool.
    Ein Hinweis ist keine Abrechnung, aber er landet vor den Augen des Arztes und
    darf deshalb genauso wenig erfunden sein.
    """
    evidence_by_id = {item.evidence_id: item for item in evidence}
    by_gop = {str(candidate["gop"]): candidate for candidate in candidates}
    by_base = {str(candidate["gop_base"]): candidate for candidate in candidates}

    hints: list[DocumentationHint] = []
    seen: set[tuple[str, str | None]] = set()
    for raw in _as_list(payload.get("documentation_hints")):
        if not isinstance(raw, dict):
            continue
        gop = canonical_gop(str(raw.get("gop") or "").strip())
        if not gop:
            continue
        gop_base, _suffix = normalize_gop(gop)
        candidate = by_gop.get(gop) or by_base.get(gop_base)
        if candidate is None:
            continue
        evidence_ids = [
            str(value) for value in _as_list(raw.get("evidence_ids")) if str(value) in evidence_by_id
        ]
        missing = [str(value) for value in _as_list(raw.get("missing_content")) if str(value).strip()]
        if not missing:
            # Ohne benannte Luecke ist der Hinweis wertlos: Er sagt dann nur, dass
            # etwas nicht ging, nicht was zu tun waere.
            continue
        service_date = _clean_optional_str(raw.get("service_date")) or _date_from_evidence(
            evidence_ids, evidence_by_id
        )
        key = (gop, service_date)
        if key in seen:
            continue
        seen.add(key)
        item_quarter = quarter_from_date(service_date) if service_date else quarter
        entry = _lookup_candidate_entry(catalog, gop, item_quarter or quarter, region, candidate)
        hints.append(
            DocumentationHint(
                gop=gop,
                gop_base=gop_base,
                title=entry.title if entry else candidate.get("title"),
                quarter=item_quarter or quarter,
                service_date=service_date,
                points=entry.points if entry else None,
                euro=entry.euro if entry else None,
                catalog_source=entry.source if entry else candidate.get("source"),
                catalog_id=entry.catalog_id if entry else candidate.get("catalog_id"),
                catalog_data_stand=entry.data_stand if entry else candidate.get("data_stand"),
                evidence_ids=evidence_ids,
                evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                covered_service_content=[
                    str(value) for value in _as_list(raw.get("covered_content")) if str(value).strip()
                ],
                missing_service_content=missing,
                reason=_clean_optional_str(raw.get("reason")),
            )
        )
    return hints


def _date_from_evidence(evidence_ids: list[str], evidence_by_id: dict[str, Evidence]) -> str | None:
    dates = sorted(
        {evidence_by_id[eid].service_date for eid in evidence_ids if evidence_by_id.get(eid) and evidence_by_id[eid].service_date}
    )
    return dates[0] if dates else None


def _quarter_from_evidence(evidence: list[Evidence]) -> str | None:
    dates = sorted(item.service_date for item in evidence if item.service_date)
    return quarter_from_date(dates[0]) if dates else None


def _collect_catalog_candidates(
    evidence: list[Evidence],
    catalog: CatalogRepository,
    quarter: str,
    region: str,
    max_candidates: int | None = None,
) -> list[dict[str, Any]]:
    settings = get_runtime_billing_rule_set(quarter, region).event_settings
    max_candidates = max_candidates or int(settings.get("max_catalog_candidates") or 120)
    search_limit = int(settings.get("catalog_search_limit") or 8)
    by_key: dict[tuple[str, str], dict[str, Any]] = {}

    def add(
        entry: CatalogEntry | None,
        evidence_ids: list[str],
        reason: str,
        support_level: str,
        requested_gop: str | None = None,
    ) -> None:
        if not entry:
            return
        gop = canonical_gop(requested_gop or entry.gop)
        gop_base, _ = normalize_gop(gop)
        if not re.fullmatch(r"\d{5}", gop_base):
            return
        key = (gop, entry.source)
        if key not in by_key:
            by_key[key] = {
                "candidate_id": f"cand-{len(by_key) + 1:03d}",
                "gop": gop,
                "gop_base": gop_base,
                "title": entry.title,
                "source": entry.source,
                "catalog_id": entry.catalog_id,
                "catalog_label": entry.catalog_label,
                "data_stand": entry.data_stand,
                "points": entry.points,
                "euro": entry.euro,
                "region": entry.region,
                "page": entry.page,
                "description": entry.description,
                "rule_texts": entry.rule_texts,
                "evidence_ids": [],
                "reason": reason,
                "support_levels": [],
            }
        by_key[key]["evidence_ids"] = sorted(set(by_key[key]["evidence_ids"] + evidence_ids))
        by_key[key]["support_levels"] = sorted(set(by_key[key]["support_levels"] + [support_level]))

    # Kein Allowlist-Seeding mehr: die Kandidaten kommen aus dem Quartalskatalog.
    # Zeit- und Sequenzregeln liefern weiterhin ihre Varianten, weil diese GOPs
    # ohne Uhrzeitkontext gar nicht auffindbar waeren.
    for item in evidence:
        # Kandidatenregeln sind unverbindlich, Zeit- und Sequenzregeln nicht.
        # Beide erreichen den Pool ueber denselben Weg und muessen unterscheidbar bleiben.
        non_binding = non_binding_gops_for_evidence_kind(item.kind, quarter, region)
        for gop in candidate_gops_for_evidence_kind(
            item.kind, quarter, region, evidence_flags=evidence_flags(item)
        ):
            add(
                catalog.lookup(gop, quarter, region),
                [item.evidence_id],
                f"billing rule for {item.kind}",
                "non_binding_candidate" if normalize_gop(gop)[0] in non_binding else "binding_rule",
                gop,
            )

        for gop in _candidate_gops(item):
            add(
                catalog.lookup(gop, quarter, region),
                [item.evidence_id],
                f"explicit candidate GOP for {item.kind}",
                "explicit_candidate",
                gop,
            )

    # Die Katalogtreffer werden reihum zugeteilt statt der Reihe nach. Sonst
    # verbraucht die erste Evidenz das Budget und spaetere Evidenzen sind im
    # Kandidatenpool ueberhaupt nicht vertreten.
    ranked_by_evidence: list[tuple[Evidence, str, list[CatalogEntry]]] = []
    for item in evidence:
        for term in _search_terms(item):
            hits = catalog.search(term, quarter, limit=search_limit)
            if hits:
                ranked_by_evidence.append((item, term, hits))

    for rank in range(search_limit):
        if len(by_key) >= max_candidates:
            break
        for item, term, hits in ranked_by_evidence:
            if rank >= len(hits):
                continue
            if len(by_key) >= max_candidates:
                break
            entry = hits[rank]
            support_level = "regional_catalog" if entry.region else "semantic_search"
            add(entry, [item.evidence_id], f"catalog text search for '{term}'", support_level)

    possible_base_gops: list[str] = []
    for item in evidence:
        possible_for_evidence = candidate_gops_for_evidence_kind(item.kind, quarter, region) + _candidate_gops(item)
        for gop in possible_for_evidence:
            decision = apply_temporal_gop_rule(
                gop,
                item.service_date,
                item.service_time,
                region,
                quarter=quarter,
            )
            if decision.gop:
                possible_base_gops.append(decision.gop)
    derived_decisions = derive_additional_gops(
        possible_base_gops,
        [item.model_dump() for item in evidence],
        quarter=quarter,
        region=region,
    )
    for derived_decision in derived_decisions:
        if not derived_decision.gop:
            continue
        add(
            catalog.lookup(derived_decision.gop, quarter, region),
            list(derived_decision.evidence_ids),
            f"derived billing rule {derived_decision.rule_id}",
            "derived_rule",
            derived_decision.gop,
        )

    candidate_values = list(by_key.values())
    derived_bases = {normalize_gop(decision.gop or "")[0] for decision in derived_decisions}
    derived_candidates = [item for item in candidate_values if item["gop_base"] in derived_bases]
    other_candidates = [item for item in candidate_values if item["gop_base"] not in derived_bases]
    return (derived_candidates + other_candidates)[:max_candidates]


def _candidate_gops(item: Evidence) -> list[str]:
    metadata_gops = item.metadata.get("candidate_gops") if isinstance(item.metadata, dict) else None
    if not isinstance(metadata_gops, list):
        return []
    return list(dict.fromkeys(canonical_gop(str(gop)) for gop in metadata_gops if str(gop).strip()))


def _search_terms(item: Evidence) -> list[str]:
    terms: list[str] = []
    metadata_terms = item.metadata.get("search_terms") if isinstance(item.metadata, dict) else None
    if isinstance(metadata_terms, list):
        terms.extend(str(term) for term in metadata_terms if str(term).strip())
    for raw in (item.label, item.text):
        cleaned = re.sub(r"[^0-9A-Za-zÄÖÜäöüß/+\- ]+", " ", raw or "")
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if len(cleaned) >= 3:
            terms.append(cleaned[:80])
    return list(dict.fromkeys(terms))


def _build_messages(
    evidence: list[Evidence],
    candidates: list[dict[str, Any]],
    quarter: str,
    region: str,
) -> list[dict[str, str]]:
    evidence_payload = [
        {
            "evidence_id": item.evidence_id,
            "kind": item.kind,
            "label": item.label,
            "page": item.page,
            "service_date": item.service_date,
            "service_time": item.service_time,
            "text": item.text,
            "confidence": item.confidence,
            "value": item.value,
            "metadata": item.metadata,
        }
        for item in evidence
    ]
    candidate_payload = [
        {
            "candidate_id": item["candidate_id"],
            "gop": item["gop"],
            "gop_base": item["gop_base"],
            "title": item["title"],
            "source": item["source"],
            "catalog_id": item.get("catalog_id"),
            "catalog_label": item.get("catalog_label"),
            "data_stand": item.get("data_stand"),
            "points": item["points"],
            "euro": item["euro"],
            "description": item.get("description"),
            "rule_texts": item.get("rule_texts") or [],
            "evidence_ids": item["evidence_ids"],
            "reason": item["reason"],
            "support_levels": item.get("support_levels") or [],
        }
        for item in candidates
    ]

    system = (
        "Du bist ein vorsichtiger medizinischer Abrechnungsassistent für EBM und regionale Hessen-GOP. "
        "Leite aus klinischer Evidenz abrechenbare GOP-Positionen ab. "
        "Nutze ausschließlich GOPs aus catalog_candidates. Erfinde keine GOPs. "
        "Bilde getrennte Positionen für getrennte Leistungsereignisse. Dieselbe GOP darf an verschiedenen "
        "Leistungstagen erneut vorkommen; mehrere technische Dokumente derselben Sitzung sind dagegen nur ein Ereignis. "
        "Ein Datumswechsel um Mitternacht beendet eine laufende Sitzung nicht automatisch. Eine Erst- oder "
        "Grundpauschale darf innerhalb einer Kontaktsequenz nur einmal vorgeschlagen werden; nur ein belegter weiterer "
        "Kontakt kann eine zeitabhängige Folgekonsultationspauschale auslösen. "
        "Bei zeitabhängigen Positionen sind Leistungsdatum, Uhrzeit, Wochentag, Feiertag sowie Erst- oder Folgekontakt zu beachten. "
        "Wenn eine Leistung nur angefordert, storniert, intern dokumentiert oder unsicher ist, nimm sie nicht als item auf, "
        "sondern als review_candidate oder excluded_evidence. "
        "Stufe jeden Kandidaten in genau eine von drei Lagen ein: belegt, dann als item; erbracht, aber "
        "die Dokumentation deckt nicht jeden obligaten Leistungsinhalt, dann als documentation_hint mit "
        "missing_content; nicht erbracht oder nicht belegt, dann als excluded_evidence. Eine Leistung, die "
        "die Akte erkennbar erwähnt, darf in keiner Antwort ganz fehlen - der mittlere Fall ist der "
        "häufigste und der wertvollste, weil er sagt, woran die Abrechnung scheitert. "
        "Kandidaten mit ausschließlich configured_candidate oder semantic_search sind nur Suchhinweise und dürfen nicht "
        "ohne zusätzliche strukturierte Evidenz als item übernommen werden. Eine ausdrücklich nicht vollständig erfüllte "
        "Leistung darf niemals als item erscheinen. "
        "Behandle Angaben wie einmal im Fall, je Sitzung oder am Behandlungstag als Abrechnungshäufigkeit. "
        "Eine Leistungsdauer darfst du nur verlangen oder zur Mengenberechnung verwenden, wenn die Kataloglegende "
        "ausdrücklich mindestens Minuten oder je vollendete Minuten nennt. "
        "Nennt die Kataloglegende einen obligaten Leistungsinhalt, führe in covered_content jedes "
        "geforderte Element wörtlich auf, das die Evidenz belegt. Lass ein Element weg, wenn die Evidenz "
        "es nicht belegt; erfinde keine Belege. "
        "Nenne jede belegte Leistung, auch wenn die Antwort dadurch länger wird. "
        "Antworte ausschließlich als JSON-Objekt."
    )
    user = {
        "task": "Erzeuge einen semantisch begründeten Rechnungsentwurf.",
        "quarter": quarter,
        "region": region,
        "billing_rules": billing_rule_guidance(),
        "json_schema": {
            "items": [
                {
                    "gop": "string",
                    "quantity": 1,
                    "evidence_ids": ["ev-..."],
                    "service_date": "YYYY-MM-DD oder null",
                    "service_time": "HH:MM oder null",
                    "confidence": "high|medium|low",
                    "reason": "kurze fachliche Herleitung",
                    "covered_content": ["wörtliches Element des obligaten Leistungsinhalts, das belegt ist"],
                }
            ],
            "documentation_hints": [
                {
                    "gop": "string",
                    "evidence_ids": ["ev-..."],
                    "service_date": "YYYY-MM-DD oder null",
                    "covered_content": ["belegtes Element des obligaten Leistungsinhalts"],
                    "missing_content": ["Element des obligaten Leistungsinhalts, das die Dokumentation nicht hergibt"],
                    "reason": "was zur Abrechnung fehlt",
                }
            ],
            "review_candidates": [
                {
                    "evidence": "string",
                    "evidence_ids": ["ev-..."],
                    "possible_gops": ["string"],
                    "reason": "string",
                }
            ],
            "excluded_evidence": [
                {
                    "evidence": "string",
                    "evidence_ids": ["ev-..."],
                    "not_billed_gop": "string oder null",
                    "reason": "string",
                }
            ],
        },
        "evidence": evidence_payload,
        "catalog_candidates": candidate_payload,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(user, ensure_ascii=False)},
    ]


def _call_mistral_chat_json(messages: list[dict[str, str]], settings: Settings) -> dict[str, Any]:
    if not settings.mistral_api_key:
        raise SemanticBillingError("MISTRAL_API_KEY ist nicht konfiguriert.")

    payload = {
        "model": settings.mistral_llm_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": settings.mistral_llm_max_tokens,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        "https://api.mistral.ai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {settings.mistral_api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            response_payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise SemanticBillingError(f"Die Mistral-Chat-Anfrage ist mit HTTP {exc.code} fehlgeschlagen: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SemanticBillingError(f"Die Mistral-Chat-Anfrage ist fehlgeschlagen: {exc}") from exc

    choices = response_payload.get("choices") or []
    if not choices:
        raise SemanticBillingError("Mistral Chat hat keine Auswahl zurückgegeben.")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise SemanticBillingError("Mistral Chat hat keinen JSON-Inhalt zurückgegeben.")
    return _json_from_text(content)


def _request_payload_with_retry(
    messages: list[dict[str, str]],
    settings: Settings,
    llm_client: LlmClient | None,
    semantic_policy: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Antwort des Modells holen und bei unbrauchbarem Ergebnis erneut fragen.

    Ohne semantische Herleitung entsteht kein einziger Rechnungsvorschlag. Ein
    einzelner Aussetzer des Modells - abgeschnittenes JSON, Prosa statt Objekt,
    ein Netzfehler - wuerde deshalb einen leeren Entwurf erzeugen. Der zweite
    Versuch bekommt den Fehler genannt, damit das Modell ihn beheben kann.

    Aufgegeben wird erst nach allen Versuchen; der Aufrufer faellt dann auf den
    leeren Pfad zurueck und die Warnung nennt jeden Versuch einzeln.
    """
    max_attempts = max(1, int(semantic_policy.get("max_attempts") or 2))
    attempts: list[dict[str, Any]] = []
    conversation = list(messages)
    last_error: SemanticBillingError | None = None

    for attempt in range(1, max_attempts + 1):
        try:
            raw = llm_client(conversation, settings) if llm_client else _call_mistral_chat_json(conversation, settings)
            payload = _coerce_json_payload(raw)
            if not isinstance(payload.get("items"), list):
                raise SemanticBillingError("Die LLM-Antwort enthält kein Feld 'items' als Liste.")
            attempts.append({"attempt": attempt, "status": "ok"})
            return payload, attempts
        except SemanticBillingError as exc:
            last_error = exc
            attempts.append({"attempt": attempt, "status": "failed", "reason": str(exc)})
            if attempt >= max_attempts:
                break
            conversation = list(messages) + [
                {
                    "role": "user",
                    "content": (
                        f"Der vorherige Versuch war unbrauchbar: {exc} "
                        "Antworte ausschließlich mit einem einzigen JSON-Objekt nach dem angegebenen Schema, "
                        "ohne einleitenden Text, ohne Markdown-Codeblock und ohne Kommentare. "
                        "Das Feld items muss vorhanden sein. Lass dabei keine belegte Leistung weg; "
                        "die Antwort soll gültig sein, nicht kurz. Enthält der Fall keine "
                        "abrechenbare Leistung, gib items als leere Liste zurück."
                    ),
                }
            ]

    detail = "; ".join(f"Versuch {a['attempt']}: {a.get('reason', '')}" for a in attempts)
    raise SemanticBillingError(f"Nach {len(attempts)} Versuchen keine verwertbare Antwort. {detail}")


def _coerce_json_payload(raw_payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw_payload, str):
        return _json_from_text(raw_payload)
    if not isinstance(raw_payload, dict):
        raise SemanticBillingError("Die LLM-Antwort ist kein JSON-Objekt.")
    return raw_payload


def _json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise SemanticBillingError("In der LLM-Antwort wurde kein JSON-Objekt gefunden.")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SemanticBillingError(f"Das LLM hat ungültiges JSON geliefert: {exc}") from exc
    if not isinstance(payload, dict):
        raise SemanticBillingError("Die JSON-Antwort des LLM muss ein Objekt sein.")
    return payload


def _billing_items_from_payload(
    payload: dict[str, Any],
    evidence: list[Evidence],
    events: list[BillingEvent],
    candidates: list[dict[str, Any]],
    catalog: CatalogRepository,
    quarter: str,
    region: str,
    agreement: dict[tuple[str, str | None], int] | None = None,
    passes: int = 1,
) -> tuple[list[BillingItem], list[ReviewCandidate]]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    candidate_by_gop: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_by_gop[canonical_gop(candidate["gop"])] = candidate
        candidate_by_gop[canonical_gop(candidate["gop_base"])] = candidate

    items: list[BillingItem] = []
    review: list[ReviewCandidate] = []
    used_event_gops: set[tuple[str, str]] = set()

    used_sequence_events: set[tuple[str, str]] = set()
    semantic_policy = get_runtime_billing_rule_set(quarter, region).semantic_policy

    for proposal in _sorted_item_proposals(payload.get("items"), evidence_by_id):
        raw_gop = str(proposal.get("gop") or "").strip().upper()
        gop = canonical_gop(raw_gop)
        if not gop:
            continue
        proposed_base, _proposed_suffix = normalize_gop(gop)
        candidate = candidate_by_gop.get(gop) or candidate_by_gop.get(proposed_base)
        evidence_ids = _valid_evidence_ids(proposal.get("evidence_ids"), evidence_by_id)
        if not evidence_ids and candidate:
            evidence_ids = [item for item in candidate.get("evidence_ids", []) if item in evidence_by_id]
        if not candidate:
            review.append(
                ReviewCandidate(
                    evidence=f"LLM-Vorschlag GOP {gop}",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                    possible_gops=[canonical_gop(gop)],
                    reason="GOP war nicht im bereitgestellten Katalog-Kandidatenpool und wurde nicht automatisch übernommen.",
                )
            )
            continue
        proposal_reason = _clean_optional_str(proposal.get("reason"))
        acceptance_reason = _non_binding_candidate_reason(candidate) or _semantic_acceptance_failure(
            candidate, proposal_reason, semantic_policy
        )
        if acceptance_reason:
            review.append(
                ReviewCandidate(
                    evidence=f"Semantischer Vorschlag GOP {gop}",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                    possible_gops=[canonical_gop(gop)],
                    reason=acceptance_reason,
                )
            )
            continue
        selected = _select_evidence_for_item(evidence_ids, evidence_by_id)
        service_date = _clean_optional_str(proposal.get("service_date")) or (selected.service_date if selected else None)
        service_time = _clean_optional_str(proposal.get("service_time")) or (selected.service_time if selected else None)
        event = _select_billing_event(events, evidence_ids, service_date, service_time)
        sequence_event = _select_sequence_event_for_proposal(
            events,
            gop,
            event,
            service_date,
            service_time,
            quarter,
            region,
        )
        if sequence_event:
            evidence_ids = list(dict.fromkeys(evidence_ids + sequence_event.evidence_ids))
            selected = _select_evidence_for_item(evidence_ids, evidence_by_id)
            if event and event.session_id == sequence_event.session_id:
                service_date, service_time = _earliest_service_datetime(
                    service_date,
                    service_time,
                    sequence_event.service_date,
                    sequence_event.service_time,
                )
            else:
                service_date = sequence_event.service_date or service_date
                service_time = sequence_event.service_time or service_time
            event = sequence_event
        if event:
            if not sequence_event:
                service_date = event.service_date or service_date
                service_time = event.service_time or service_time
            if event.sequence_gop:
                gop = canonical_gop(event.sequence_gop)
        item_quarter = quarter_from_date(service_date) or quarter

        temporal_decision = apply_temporal_gop_rule(
            gop,
            service_date,
            service_time,
            region,
            quarter=item_quarter,
        )
        validation_notes = list(temporal_decision.notes)
        if temporal_decision.gop and temporal_decision.gop != gop:
            corrected_base, _corrected_suffix = normalize_gop(temporal_decision.gop)
            corrected_candidate = candidate_by_gop.get(temporal_decision.gop) or candidate_by_gop.get(corrected_base)
            if not corrected_candidate:
                review.append(
                    ReviewCandidate(
                        evidence=f"Zeitabhängiger LLM-Vorschlag GOP {gop}",
                        evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                        possible_gops=[canonical_gop(gop), canonical_gop(temporal_decision.gop)],
                        reason=(
                            f"Nach Datum/Uhrzeit wäre {temporal_decision.gop} plausibel, "
                            "diese GOP war aber nicht im Katalog-Kandidatenpool."
                        ),
                    )
                )
                continue
            candidate = corrected_candidate
            gop = canonical_gop(temporal_decision.gop)

        gop_base, gop_suffix = normalize_gop(gop)
        if not candidate:
            candidate = candidate_by_gop.get(gop) or candidate_by_gop.get(gop_base) or candidate_by_gop.get(proposed_base)

        if not candidate:
            review.append(
                ReviewCandidate(
                    evidence=f"LLM-Vorschlag GOP {gop}",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                    possible_gops=[canonical_gop(gop)],
                    reason="GOP war nicht im bereitgestellten Katalog-Kandidatenpool und wurde nicht automatisch übernommen.",
                )
            )
            continue
        event_key = event.event_id if event else f"{service_date or 'unknown'}T{service_time or 'unknown'}"
        sequence_key = (
            (event.sequence_rule_id, event.event_id)
            if event and event.sequence_rule_id
            else None
        )
        if sequence_key and sequence_key in used_sequence_events:
            review.append(
                ReviewCandidate(
                    evidence=f"Doppelter Vorschlag einer Kontaktpauschale ({gop})",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                    possible_gops=[canonical_gop(gop)],
                    reason=(
                        "Für dasselbe zeitliche Kontakt- und Sequenzereignis wurde bereits eine "
                        "Basisposition übernommen."
                    ),
                )
            )
            continue
        dedupe_key = (gop_base, event_key)
        if dedupe_key in used_event_gops:
            review.append(
                ReviewCandidate(
                    evidence=f"Doppelter LLM-Vorschlag GOP {gop}",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                    possible_gops=[canonical_gop(gop)],
                    reason="GOP-Basis wurde für dasselbe Leistungsereignis bereits als Rechnungsposition übernommen.",
                )
            )
            continue
        used_event_gops.add(dedupe_key)
        if sequence_key:
            used_sequence_events.add(sequence_key)

        covered = [str(value) for value in (proposal.get("covered_content") or []) if str(value).strip()]
        entry = _lookup_candidate_entry(catalog, gop, item_quarter, region, candidate)
        if not entry:
            validation_status = "catalog_missing"
            validation_notes.append(f"GOP {gop_base} wurde im Katalog {item_quarter} nicht gefunden.")
            title = candidate["title"]
            points = None
            amount = None
            source = candidate.get("source") or "UNKNOWN"
            source_label = candidate.get("catalog_label")
            catalog_id = candidate.get("catalog_id")
            catalog_data_stand = candidate.get("data_stand")
        else:
            confidence = str(proposal.get("confidence") or "medium").lower()
            validation_status = "review" if confidence == "low" else "valid"
            if temporal_decision.review_required:
                validation_status = "review"
            title = entry.title
            points = entry.points
            amount = entry.euro
            source = entry.source
            source_label = entry.catalog_label
            catalog_id = entry.catalog_id
            catalog_data_stand = entry.data_stand

        catalog_decision = evaluate_catalog_context_rules(
            BillingRuleContext(
                gop=gop,
                service_date=service_date,
                service_time=service_time,
                region=region,
                evidence_kind=selected.kind if selected else None,
                evidence_text=selected.text if selected else "",
                evidence_metadata=selected.metadata if selected else {},
                catalog_rule_texts=_candidate_rule_texts(candidate, entry),
            )
        )
        validation_notes.extend(catalog_decision.notes)
        if catalog_decision.review_required and validation_status != "catalog_missing":
            validation_status = "review"

        quantity = _safe_quantity(proposal.get("quantity"))
        confidence = str(proposal.get("confidence") or "medium").lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"
        # Bei mehreren Durchgaengen zaehlt die Uebereinstimmung mehr als die
        # Selbsteinschaetzung: Eine Position, die nur eine Minderheit der
        # Durchgaenge kennt, geht zur Pruefung, statt sich selbst hoch zu bewerten.
        if passes > 1 and agreement:
            anchor = _clean_optional_str(proposal.get("service_date")) or ",".join(
                sorted(str(value) for value in _as_list(proposal.get("evidence_ids")))
            )
            found_in = agreement.get((canonical_gop(gop), anchor or None), 0)
            # Ab welchem Anteil eine Position als getragen gilt, ist eine fachliche
            # Festlegung und keine Eigenschaft des Codes: Wer lieber mehr vorgelegt
            # bekommt, hebt den Wert im Regelwerk an.
            required_share = semantic_policy.get("min_pass_agreement_share")
            required_share = float(required_share) if isinstance(required_share, (int, float)) else 0.5
            if found_in and found_in / passes <= required_share:
                confidence = "low"
                validation_status = "review"
                validation_notes.append(
                    f"Nur {found_in} von {passes} Ableitungsdurchgängen haben diese Position "
                    "vorgeschlagen; sie wird deshalb zur Prüfung vorgelegt."
                )
        temporal_rule_suffix = f"+{temporal_decision.rule_id}" if temporal_decision.temporal_rule_id else ""
        sequence_rule_suffix = f"+{event.sequence_rule_id}" if event and event.sequence_rule_id else ""
        catalog_rule_suffix = (
            f"+{catalog_decision.rule_id}" if catalog_decision.rule_id != "catalog.context.noop.v1" else ""
        )

        items.append(
            BillingItem(
                line=len(items) + 1,
                gop_original=gop,
                gop_base=gop_base,
                gop_suffix=gop_suffix,
                title=title,
                catalog_source=source,
                catalog_source_label=source_label,
                catalog_id=catalog_id,
                catalog_data_stand=catalog_data_stand,
                quarter=item_quarter,
                service_date=service_date,
                service_time=service_time,
                service_event_id=event.event_id if event else None,
                service_session_id=event.session_id if event else None,
                treatment_episode_id=event.episode_id if event else None,
                temporal_role=event.temporal_role if event else "service_event",
                temporal_reason=event.temporal_reason if event else None,
                temporal_rule_id=temporal_decision.temporal_rule_id,
                covered_service_content=covered,
                quantity=quantity,
                points=points,
                amount_eur=amount,
                rule_id=(
                    f"semantic_llm.{gop_base}.v1{sequence_rule_suffix}{temporal_rule_suffix}{catalog_rule_suffix}"
                ),
                confidence=confidence,
                evidence_ids=evidence_ids,
                evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                validation_status=validation_status,  # type: ignore[arg-type]
                validation_notes=validation_notes,
                derivation_source="semantic_llm",
                semantic_reason=proposal_reason,
                semantic_catalog_candidates=[candidate["candidate_id"]],
            )
        )

    return items, review


def _non_binding_candidate_reason(candidate: dict[str, Any]) -> str | None:
    """Kandidatenregeln sind ausdruecklich unverbindlich.

    Sie fuehren mehrdeutige Evidenz - etwa einen internen Leistungscode oder einen
    dokumentierten Befund - auf moegliche GOPs, ohne dass die Zuordnung feststeht.
    Wird eine solche GOP nur ueber diesen Weg gefunden, ist sie ein Vorschlag zur
    Pruefung und keine Rechnungsposition.
    """
    levels = {str(value) for value in candidate.get("support_levels") or []}
    if levels and levels == {"non_binding_candidate"}:
        return (
            "Die GOP stammt ausschließlich aus einer unverbindlichen Kandidatenregel; "
            "sie wird zur Prüfung vorgelegt, nicht automatisch abgerechnet."
        )
    return None


def _semantic_acceptance_failure(
    candidate: dict[str, Any],
    proposal_reason: str | None,
    semantic_policy: dict[str, Any],
) -> str | None:
    """Frueher Filter vor der Katalogpruefung.

    Das Abrechnungstor sind die Katalogklauseln des Quartals, nicht mehr die
    Herkunft des Kandidaten. Hier faellt nur heraus, was die LLM-Begruendung
    selbst als nicht erfuellte Voraussetzung beschreibt.
    """
    if not proposal_reason:
        return None
    for pattern in semantic_policy.get("proposal_rejection_patterns") or []:
        if re.search(str(pattern), proposal_reason, re.IGNORECASE | re.DOTALL):
            return (
                "Die LLM-Herleitung beschreibt selbst eine nicht erfüllte oder fehlende "
                "Abrechnungsvoraussetzung; der Vorschlag wurde deshalb nicht übernommen."
            )
    return None


# Der Klauseltyp, der eine Dokumentationsluecke beschreibt statt eines Verbots.
CONTENT_CLAUSE_TYPE = "required_service_content"


def _split_items_by_catalog_verdict(
    items: list[BillingItem],
    validation_results: list[dict[str, Any]],
) -> tuple[list[BillingItem], list[ReviewCandidate], list[DocumentationHint]]:
    """Katalogurteil als Abrechnungstor anwenden.

    Positionen mit verletzter, maschinell entscheidbarer Klausel werden zu
    Review-Kandidaten. Unentscheidbare Klauseln bleiben als Pruefhinweis an der
    Position; sie verhindern die Abrechnung nicht.

    Konzept 3.4 zieht eine dritte Linie: Scheitert eine Position ausschliesslich
    daran, dass ein obligater Leistungsinhalt nicht belegt ist, ist das kein
    Abrechnungsausschluss, sondern eine Luecke in der Dokumentation. Sie wird
    nicht verworfen, sondern mit dem fehlenden Element benannt - das ist der Fall,
    in dem der Arzt etwas tun kann.
    """
    blocked: dict[tuple[str, str | None], list[str]] = {}
    gaps: dict[tuple[str, str | None], list[str]] = {}
    kinds: dict[tuple[str, str | None], set[str]] = {}
    for result in validation_results:
        for verdict in result.get("item_verdicts") or []:
            key = (str(verdict.get("gop_original")), verdict.get("service_event_id"))
            for element in verdict.get("content_gaps") or []:
                if str(element) not in gaps.setdefault(key, []):
                    gaps[key].append(str(element))
            kinds.setdefault(key, set()).update(
                str(kind) for kind in verdict.get("violation_clause_types") or []
            )
            if verdict.get("billable"):
                continue
            blocked.setdefault(key, []).extend(str(note) for note in verdict.get("violations") or [])

    kept: list[BillingItem] = []
    review: list[ReviewCandidate] = []
    hints: list[DocumentationHint] = []
    for item in items:
        key = (item.gop_original, item.service_event_id)
        violations = blocked.get(key)
        missing = gaps.get(key) or []
        if not violations:
            # Die Luecke blockiert hier nicht - sichtbar sein muss sie trotzdem.
            item.missing_service_content = missing
            kept.append(item)
            continue
        # Nur wenn jede Verletzung von der Inhaltsklausel stammt, ist es eine
        # Dokumentationsluecke. Kommt ein Ausschluss oder eine fehlende Genehmigung
        # dazu, bleibt es ein Fall fuer die Pruefung.
        only_content = bool(missing) and all(
            any(element[:80] in note for element in missing) for note in violations
        )
        if only_content:
            hints.append(
                DocumentationHint(
                    gop=item.gop_original,
                    gop_base=item.gop_base,
                    title=item.title,
                    quarter=item.quarter,
                    service_date=item.service_date,
                    points=item.points,
                    euro=item.amount_eur,
                    catalog_source=item.catalog_source,
                    catalog_id=item.catalog_id,
                    catalog_data_stand=item.catalog_data_stand,
                    evidence_ids=list(item.evidence_ids),
                    evidence_pages=list(item.evidence_pages),
                    covered_service_content=list(item.covered_service_content),
                    missing_service_content=missing,
                    confidence=item.confidence,
                    reason=(
                        "Die Leistung ist belegt, die Dokumentation deckt aber nicht jeden obligaten "
                        "Leistungsinhalt. Wird das Fehlende nachgetragen, ist die Position abrechenbar."
                    ),
                    origin="catalog_content_gap",
                )
            )
            continue
        review.append(
            ReviewCandidate(
                evidence=f"Katalogprüfung GOP {item.gop_original}",
                evidence_pages=list(item.evidence_pages),
                evidence_ids=list(item.evidence_ids),
                possible_gops=[item.gop_original],
                reason=(
                    "Die Position wurde nicht übernommen, weil eine Katalogbedingung des Quartals "
                    f"verletzt ist: {' '.join(violations)}"
                ),
            )
        )
    for index, item in enumerate(kept, start=1):
        item.line = index
    return kept, review, hints


def _select_billing_event(
    events: list[BillingEvent],
    evidence_ids: list[str],
    service_date: str | None,
    service_time: str | None,
) -> BillingEvent | None:
    matches = events_for_evidence_ids(events, evidence_ids)
    if service_date:
        dated = [event for event in matches if event.service_date == service_date]
        if dated:
            matches = dated
    if service_time:
        timed = [event for event in matches if event.service_time == service_time]
        if timed:
            matches = timed
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda event: (
            1 if event.sequence_gop else 0,
            1 if event.service_date else 0,
            event.service_date or "",
            event.service_time or "",
            len(event.evidence),
        ),
        reverse=True,
    )[0]


def _select_sequence_event_for_proposal(
    events: list[BillingEvent],
    gop: str,
    reference_event: BillingEvent | None,
    service_date: str | None,
    service_time: str | None,
    quarter: str,
    region: str,
) -> BillingEvent | None:
    definitions = get_runtime_billing_rule_set(quarter, region)
    gop_base, _ = normalize_gop(gop)
    for rule in definitions.event_sequence_rules:
        if not definition_is_applicable(rule.valid_from, rule.valid_to, rule.regions, quarter, region):
            continue
        if gop_base not in _sequence_gop_family(rule.rule_id, definitions):
            continue
        matching = [
            event
            for event in events
            if event.primary_episode and event.sequence_rule_id == rule.rule_id
        ]
        if not matching:
            continue
        if reference_event in matching:
            return reference_event
        if reference_event and reference_event.session_id:
            same_session = [event for event in matching if event.session_id == reference_event.session_id]
            if same_session:
                return min(same_session, key=_sequence_event_sort_key)
        requested = _parse_service_datetime(service_date, service_time)
        if requested:
            timed = [
                (abs((event_time - requested).total_seconds()), event)
                for event in matching
                if (event_time := _parse_service_datetime(event.service_date, event.service_time))
            ]
            if timed:
                distance, nearest = min(timed, key=lambda value: value[0])
                if distance <= rule.session_gap_minutes * 60:
                    return nearest
        if len(matching) == 1 and not service_date:
            return matching[0]
    return None


def _sequence_gop_family(rule_id: str, definitions: BillingRuleSet) -> set[str]:
    sequence_rule = next(rule for rule in definitions.event_sequence_rules if rule.rule_id == rule_id)
    family = {normalize_gop(sequence_rule.initial_gop)[0], normalize_gop(sequence_rule.subsequent_gop)[0]}
    changed = True
    while changed:
        changed = False
        for temporal_rule in definitions.temporal_rules:
            temporal_gops = {normalize_gop(value)[0] for value in temporal_rule.gops}
            if not family.intersection(temporal_gops):
                continue
            expanded = temporal_gops | {normalize_gop(outcome.gop)[0] for outcome in temporal_rule.outcomes}
            if not expanded.issubset(family):
                family.update(expanded)
                changed = True
    return family


def _sorted_item_proposals(value: Any, evidence_by_id: dict[str, Evidence]) -> list[dict[str, Any]]:
    proposals = _as_list(value)

    def sort_key(indexed: tuple[int, dict[str, Any]]) -> tuple[str, str, int]:
        index, proposal = indexed
        evidence_ids = _valid_evidence_ids(proposal.get("evidence_ids"), evidence_by_id)
        evidence_times = sorted(
            (
                item.service_date,
                item.service_time or "23:59",
            )
            for evidence_id in evidence_ids
            if (item := evidence_by_id[evidence_id]).service_date
        )
        fallback_date, fallback_time = evidence_times[0] if evidence_times else ("9999-12-31", "23:59")
        return (
            _clean_optional_str(proposal.get("service_date")) or fallback_date,
            _clean_optional_str(proposal.get("service_time")) or fallback_time,
            index,
        )

    return [proposal for _index, proposal in sorted(enumerate(proposals), key=sort_key)]


def _earliest_service_datetime(
    first_date: str | None,
    first_time: str | None,
    second_date: str | None,
    second_time: str | None,
) -> tuple[str | None, str | None]:
    first = _parse_service_datetime(first_date, first_time)
    second = _parse_service_datetime(second_date, second_time)
    if first and second:
        return (first_date, first_time) if first <= second else (second_date, second_time)
    if first:
        return first_date, first_time
    return second_date, second_time


def _parse_service_datetime(date_value: str | None, time_value: str | None) -> datetime | None:
    if not date_value or not time_value:
        return None
    try:
        return datetime.fromisoformat(f"{date_value}T{time_value}")
    except ValueError:
        return None


def _sequence_event_sort_key(event: BillingEvent) -> tuple[str, str, str]:
    return event.service_date or "9999-12-31", event.service_time or "23:59", event.event_id


def _lookup_candidate_entry(
    catalog: CatalogRepository,
    gop: str,
    quarter: str,
    region: str,
    candidate: dict[str, Any],
) -> CatalogEntry | None:
    source = candidate.get("source")
    if source == "KV_HESSEN_GOP":
        return catalog.lookup_hessen(gop, quarter, region)
    if source == "EBM_KBV":
        return catalog.lookup_ebm(gop, quarter)
    return catalog.lookup(gop, quarter, region=region)


def _candidate_rule_texts(candidate: dict[str, Any], entry: CatalogEntry | None) -> tuple[str, ...]:
    texts: list[str] = []
    if entry:
        if entry.description:
            texts.append(entry.description)
        texts.extend(entry.rule_texts)
    description = candidate.get("description")
    if description:
        texts.append(str(description))
    for text in _as_list(candidate.get("rule_texts")):
        if str(text).strip():
            texts.append(str(text))
    return tuple(dict.fromkeys(texts))


def _review_from_payload(payload: dict[str, Any], evidence: list[Evidence]) -> list[ReviewCandidate]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    result: list[ReviewCandidate] = []
    for item in _as_list(payload.get("review_candidates")):
        evidence_ids = _valid_evidence_ids(item.get("evidence_ids"), evidence_by_id)
        result.append(
            ReviewCandidate(
                evidence=str(item.get("evidence") or "LLM-Review-Kandidat"),
                evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                possible_gops=[canonical_gop(str(gop)) for gop in _as_list(item.get("possible_gops")) if str(gop).strip()],
                reason=str(item.get("reason") or "Semantisch unsicher; manuelle Prüfung erforderlich."),
            )
        )
    return result


def _excluded_from_payload(payload: dict[str, Any], evidence: list[Evidence]) -> list[ExcludedEvidence]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    result: list[ExcludedEvidence] = []
    for item in _as_list(payload.get("excluded_evidence")):
        evidence_ids = _valid_evidence_ids(item.get("evidence_ids"), evidence_by_id)
        result.append(
            ExcludedEvidence(
                evidence=str(item.get("evidence") or "Nicht übernommene Evidenz"),
                evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    evidence_ids=list(evidence_ids),
                not_billed_gop=_canonical_optional_gop(item.get("not_billed_gop")),
                reason=str(item.get("reason") or "Semantisch ausgeschlossen."),
            )
        )
    return result


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _valid_evidence_ids(value: Any, evidence_by_id: dict[str, Evidence]) -> list[str]:
    ids = [str(item) for item in _as_list(value)]
    return [item for item in dict.fromkeys(ids) if item in evidence_by_id]


def _pages_for_ids(evidence_ids: list[str], evidence_by_id: dict[str, Evidence]) -> list[int]:
    return sorted({evidence_by_id[item].page for item in evidence_ids if item in evidence_by_id})


def _select_evidence_for_item(evidence_ids: list[str], evidence_by_id: dict[str, Evidence]) -> Evidence | None:
    candidates = [evidence_by_id[item] for item in evidence_ids if item in evidence_by_id]
    if not candidates:
        return None

    def score(item: Evidence) -> tuple[int, str, float]:
        has_date = 1 if item.service_date else 0
        service_datetime = f"{item.service_date or ''}T{item.service_time or '00:00'}"
        return has_date, service_datetime, item.confidence

    return sorted(candidates, key=score, reverse=True)[0]


def _clean_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() == "null" else text


def _canonical_optional_gop(value: Any) -> str | None:
    text = _clean_optional_str(value)
    return canonical_gop(text) if text else None


def _safe_quantity(value: Any) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return 1
    return max(quantity, 1)
