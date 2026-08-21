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
    derive_additional_gops,
    evidence_billing_rules,
    evaluate_catalog_context_rules,
)
from .billing_events import (
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
from .models import BillingItem, CatalogEntry, Evidence, ExcludedEvidence, InvoiceSummary, ReviewCandidate
from .rule_engine import append_derived_billing_items, generate_billing_items


class SemanticBillingError(RuntimeError):
    pass


LlmClient = Callable[[list[dict[str, str]], Settings], Union[dict[str, Any], str]]


@dataclass(frozen=True)
class SemanticBillingResult:
    items: list[BillingItem]
    summary: InvoiceSummary
    review_candidates: list[ReviewCandidate]
    excluded_evidence: list[ExcludedEvidence]
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

    quarter = default_quarter or _quarter_from_evidence(evidence) or "2025/Q4"
    events = build_billing_events(evidence, quarter, region)
    billing_evidence = primary_episode_evidence(events)
    candidates = _collect_catalog_candidates(billing_evidence, catalog, quarter, region)
    if not candidates:
        raise SemanticBillingError(f"Für das Quartal {quarter} wurden keine Katalogkandidaten gefunden.")

    messages = _build_messages(billing_evidence, candidates, quarter, region)
    raw_payload = llm_client(messages, settings) if llm_client else _call_mistral_chat_json(messages, settings)
    payload = _coerce_json_payload(raw_payload)

    items, item_review = _billing_items_from_payload(payload, billing_evidence, events, candidates, catalog, quarter, region)
    rule_set = get_runtime_billing_rule_set(quarter, region)
    if rule_set.semantic_policy.get("ensure_direct_rule_items", True):
        deterministic_items, _ = generate_billing_items(billing_evidence, catalog, quarter, region)
        _append_missing_rule_backed_items(items, deterministic_items)
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
    finalize_billing_timeline(items)
    review = item_review + _review_from_payload(payload, billing_evidence)
    excluded = _excluded_from_payload(payload, billing_evidence)
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
        context={
            "mode": "semantic_llm",
            "provider": "mistral",
            "model": settings.mistral_llm_model,
            "quarter": quarter,
            "region": region,
            "catalog_candidate_count": len(candidates),
            "billing_event_count": len(events),
            "episode_selection": episode_selection_payload(events),
            "catalog_rule_validation": catalog_rule_validation,
        },
    )


def _quarter_from_evidence(evidence: list[Evidence]) -> str | None:
    dates = sorted(item.service_date for item in evidence if item.service_date)
    return quarter_from_date(dates[0]) if dates else None


def _collect_catalog_candidates(
    evidence: list[Evidence],
    catalog: CatalogRepository,
    quarter: str,
    region: str,
    max_candidates: int = 80,
) -> list[dict[str, Any]]:
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

    rules_by_kind: dict[str, list[str]] = {}
    for rule in evidence_billing_rules(quarter, region):
        rules_by_kind.setdefault(rule.evidence_kind, []).append(rule.gop)
    trusted_variants = _trusted_rule_variants(rules_by_kind, quarter, region)

    for item in evidence:
        for gop in rules_by_kind.get(item.kind, []):
            add(
                catalog.lookup(gop, quarter, region),
                [item.evidence_id],
                f"validated prior rule for {item.kind}",
                "validated_rule",
                gop,
            )

        for gop in candidate_gops_for_evidence_kind(item.kind, quarter, region):
            support_level = (
                "validated_rule_variant"
                if canonical_gop(gop) in trusted_variants.get(item.kind, set())
                else "configured_candidate"
            )
            add(
                catalog.lookup(gop, quarter, region),
                [item.evidence_id],
                f"time-dependent billing rule for {item.kind}",
                support_level,
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

    for item in evidence:
        for term in _search_terms(item):
            for entry in catalog.search(term, quarter, limit=8):
                support_level = "regional_catalog" if entry.region else "semantic_search"
                add(entry, [item.evidence_id], f"catalog text search for '{term}'", support_level)

        if len(by_key) >= max_candidates:
            break

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


def _trusted_rule_variants(
    rules_by_kind: dict[str, list[str]],
    quarter: str,
    region: str,
) -> dict[str, set[str]]:
    rule_set = get_runtime_billing_rule_set(quarter, region)
    result = {kind: {canonical_gop(gop) for gop in gops} for kind, gops in rules_by_kind.items()}
    for sequence_rule in rule_set.event_sequence_rules:
        if not definition_is_applicable(
            sequence_rule.valid_from,
            sequence_rule.valid_to,
            sequence_rule.regions,
            quarter,
            region,
        ):
            continue
        for kind in sequence_rule.evidence_kinds:
            if kind not in result:
                continue
            result[kind].update(
                canonical_gop(gop)
                for gop in (sequence_rule.initial_gop, sequence_rule.subsequent_gop)
            )
    for kind, trusted_gops in result.items():
        trusted_bases = {normalize_gop(gop)[0] for gop in trusted_gops}
        for temporal_rule in rule_set.temporal_rules:
            if not definition_is_applicable(
                temporal_rule.valid_from,
                temporal_rule.valid_to,
                temporal_rule.regions,
                quarter,
                region,
            ):
                continue
            if trusted_bases.intersection(normalize_gop(gop)[0] for gop in temporal_rule.gops):
                trusted_gops.update(canonical_gop(outcome.gop) for outcome in temporal_rule.outcomes)
    return result


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
        "Kandidaten mit ausschließlich configured_candidate oder semantic_search sind nur Suchhinweise und dürfen nicht "
        "ohne zusätzliche strukturierte Evidenz als item übernommen werden. Eine ausdrücklich nicht vollständig erfüllte "
        "Leistung darf niemals als item erscheinen. "
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
        "max_tokens": 3000,
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
                    possible_gops=[canonical_gop(gop)],
                    reason="GOP war nicht im bereitgestellten Katalog-Kandidatenpool und wurde nicht automatisch übernommen.",
                )
            )
            continue
        proposal_reason = _clean_optional_str(proposal.get("reason"))
        acceptance_reason = _semantic_acceptance_failure(candidate, proposal_reason, semantic_policy)
        if acceptance_reason:
            review.append(
                ReviewCandidate(
                    evidence=f"Semantischer Vorschlag GOP {gop}",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
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
                    possible_gops=[canonical_gop(gop)],
                    reason="GOP-Basis wurde für dasselbe Leistungsereignis bereits als Rechnungsposition übernommen.",
                )
            )
            continue
        used_event_gops.add(dedupe_key)
        if sequence_key:
            used_sequence_events.add(sequence_key)

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
        temporal_rule_suffix = f"+{temporal_decision.rule_id}" if temporal_decision.rule_id.startswith("time.") else ""
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


def _semantic_acceptance_failure(
    candidate: dict[str, Any],
    proposal_reason: str | None,
    semantic_policy: dict[str, Any],
) -> str | None:
    support_levels = {str(value) for value in candidate.get("support_levels") or []}
    accepted_levels = {
        str(value)
        for value in semantic_policy.get(
            "auto_accept_support_levels",
            ["validated_rule", "validated_rule_variant", "derived_rule", "explicit_candidate", "regional_catalog"],
        )
    }
    if not support_levels.intersection(accepted_levels):
        return (
            "Der Vorschlag beruht nur auf einem internen oder semantischen Kataloghinweis. "
            "Für eine automatische Übernahme fehlt eine validierte Leistungsregel oder strukturierte GOP-Evidenz."
        )
    if proposal_reason:
        for pattern in semantic_policy.get("proposal_rejection_patterns") or []:
            if re.search(str(pattern), proposal_reason, re.IGNORECASE | re.DOTALL):
                return (
                    "Die LLM-Herleitung beschreibt selbst eine nicht erfüllte oder fehlende "
                    "Abrechnungsvoraussetzung; der Vorschlag wurde deshalb nicht übernommen."
                )
    return None


def _append_missing_rule_backed_items(items: list[BillingItem], deterministic_items: list[BillingItem]) -> None:
    existing = {
        (
            item.gop_base,
            item.service_event_id or f"{item.service_date or ''}T{item.service_time or ''}",
        )
        for item in items
    }
    for item in deterministic_items:
        key = (
            item.gop_base,
            item.service_event_id or f"{item.service_date or ''}T{item.service_time or ''}",
        )
        if key in existing:
            continue
        items.append(item)
        existing.add(key)
    for line, item in enumerate(items, start=1):
        item.line = line


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
