from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Union

from .catalog import CatalogRepository, normalize_gop
from .config import Settings
from .evidence_extraction import quarter_from_date
from .models import BillingItem, CatalogEntry, Evidence, ExcludedEvidence, InvoiceSummary, ReviewCandidate
from .rule_engine import ACTIVE_RULES


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
        raise SemanticBillingError("Semantic billing is disabled")
    if not settings.mistral_api_key and llm_client is None:
        raise SemanticBillingError("MISTRAL_API_KEY is not configured")

    quarter = default_quarter or _quarter_from_evidence(evidence) or "2025/Q4"
    candidates = _collect_catalog_candidates(evidence, catalog, quarter, region)
    if not candidates:
        raise SemanticBillingError(f"No catalog candidates found for quarter {quarter}")

    messages = _build_messages(evidence, candidates, quarter, region)
    raw_payload = llm_client(messages, settings) if llm_client else _call_mistral_chat_json(messages, settings)
    payload = _coerce_json_payload(raw_payload)

    items, item_review = _billing_items_from_payload(payload, evidence, candidates, catalog, quarter, region)
    review = item_review + _review_from_payload(payload, evidence)
    excluded = _excluded_from_payload(payload, evidence)
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

    def add(entry: CatalogEntry | None, evidence_ids: list[str], reason: str, requested_gop: str | None = None) -> None:
        if not entry:
            return
        gop = requested_gop or entry.gop
        gop_base, _ = normalize_gop(gop)
        key = (gop, entry.source)
        if key not in by_key:
            by_key[key] = {
                "candidate_id": f"cand-{len(by_key) + 1:03d}",
                "gop": gop,
                "gop_base": gop_base,
                "title": entry.title,
                "source": entry.source,
                "points": entry.points,
                "euro": entry.euro,
                "region": entry.region,
                "page": entry.page,
                "evidence_ids": [],
                "reason": reason,
            }
        by_key[key]["evidence_ids"] = sorted(set(by_key[key]["evidence_ids"] + evidence_ids))

    rules_by_kind: dict[str, list[str]] = {}
    for rule in ACTIVE_RULES:
        rules_by_kind.setdefault(rule.evidence_kind, []).append(rule.gop_original)

    for item in evidence:
        for gop in rules_by_kind.get(item.kind, []):
            add(catalog.lookup(gop, quarter, region), [item.evidence_id], f"validated prior rule for {item.kind}", gop)

        for term in _search_terms(item):
            for entry in catalog.search(term, quarter, limit=8):
                add(entry, [item.evidence_id], f"catalog text search for '{term}'")

        if len(by_key) >= max_candidates:
            break

    return list(by_key.values())[:max_candidates]


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
            "points": item["points"],
            "euro": item["euro"],
            "evidence_ids": item["evidence_ids"],
            "reason": item["reason"],
        }
        for item in candidates
    ]

    system = (
        "Du bist ein vorsichtiger medizinischer Abrechnungsassistent fuer EBM und regionale Hessen-GOP. "
        "Leite aus klinischer Evidenz abrechenbare GOP-Positionen ab. "
        "Nutze ausschliesslich GOPs aus catalog_candidates. Erfinde keine GOPs. "
        "Wenn eine Leistung nur angefordert, storniert, intern dokumentiert oder unsicher ist, nimm sie nicht als item auf, "
        "sondern als review_candidate oder excluded_evidence. "
        "Antworte ausschliesslich als JSON-Objekt."
    )
    user = {
        "task": "Erzeuge einen semantisch begruendeten Rechnungsentwurf.",
        "quarter": quarter,
        "region": region,
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
        raise SemanticBillingError("MISTRAL_API_KEY is not configured")

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
        raise SemanticBillingError(f"Mistral chat request failed with HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise SemanticBillingError(f"Mistral chat request failed: {exc}") from exc

    choices = response_payload.get("choices") or []
    if not choices:
        raise SemanticBillingError("Mistral chat returned no choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    if not isinstance(content, str) or not content.strip():
        raise SemanticBillingError("Mistral chat returned no JSON content")
    return _json_from_text(content)


def _coerce_json_payload(raw_payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(raw_payload, str):
        return _json_from_text(raw_payload)
    if not isinstance(raw_payload, dict):
        raise SemanticBillingError("LLM payload is not a JSON object")
    return raw_payload


def _json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end < start:
        raise SemanticBillingError("No JSON object found in LLM response")
    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise SemanticBillingError(f"Invalid JSON from LLM: {exc}") from exc
    if not isinstance(payload, dict):
        raise SemanticBillingError("LLM JSON response must be an object")
    return payload


def _billing_items_from_payload(
    payload: dict[str, Any],
    evidence: list[Evidence],
    candidates: list[dict[str, Any]],
    catalog: CatalogRepository,
    quarter: str,
    region: str,
) -> tuple[list[BillingItem], list[ReviewCandidate]]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    candidate_by_gop: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        candidate_by_gop[candidate["gop"].upper()] = candidate
        candidate_by_gop[candidate["gop_base"].upper()] = candidate

    items: list[BillingItem] = []
    review: list[ReviewCandidate] = []
    used_bases: set[str] = set()

    for proposal in _as_list(payload.get("items")):
        gop = str(proposal.get("gop") or "").strip().upper()
        if not gop:
            continue
        gop_base, gop_suffix = normalize_gop(gop)
        candidate = candidate_by_gop.get(gop) or candidate_by_gop.get(gop_base)
        evidence_ids = _valid_evidence_ids(proposal.get("evidence_ids"), evidence_by_id)
        if not evidence_ids and candidate:
            evidence_ids = [item for item in candidate.get("evidence_ids", []) if item in evidence_by_id]

        if not candidate:
            review.append(
                ReviewCandidate(
                    evidence=f"LLM-Vorschlag GOP {gop}",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    possible_gops=[gop],
                    reason="GOP war nicht im bereitgestellten Katalog-Kandidatenpool und wurde nicht automatisch uebernommen.",
                )
            )
            continue
        if gop_base in used_bases:
            review.append(
                ReviewCandidate(
                    evidence=f"Doppelter LLM-Vorschlag GOP {gop}",
                    evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                    possible_gops=[gop],
                    reason="GOP-Basis wurde bereits als Rechnungsposition uebernommen.",
                )
            )
            continue
        used_bases.add(gop_base)

        entry = catalog.lookup(gop, quarter, region=region)
        validation_notes: list[str] = []
        if not entry:
            validation_status = "catalog_missing"
            validation_notes.append(f"GOP {gop_base} wurde im Katalog {quarter} nicht gefunden.")
            title = candidate["title"]
            points = None
            amount = None
            source = "UNKNOWN"
        else:
            confidence = str(proposal.get("confidence") or "medium").lower()
            validation_status = "review" if confidence == "low" else "valid"
            title = entry.title
            points = entry.points
            amount = entry.euro
            source = entry.source

        selected = _select_evidence_for_item(evidence_ids, evidence_by_id)
        service_date = _clean_optional_str(proposal.get("service_date")) or (selected.service_date if selected else None)
        service_time = _clean_optional_str(proposal.get("service_time")) or (selected.service_time if selected else None)
        quantity = _safe_quantity(proposal.get("quantity"))
        confidence = str(proposal.get("confidence") or "medium").lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "medium"

        items.append(
            BillingItem(
                line=len(items) + 1,
                gop_original=gop,
                gop_base=gop_base,
                gop_suffix=gop_suffix,
                title=title,
                catalog_source=source,
                quarter=quarter,
                service_date=service_date,
                service_time=service_time,
                quantity=quantity,
                points=points,
                amount_eur=amount,
                rule_id=f"semantic_llm.{gop_base}.v1",
                confidence=confidence,
                evidence_ids=evidence_ids,
                evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                validation_status=validation_status,  # type: ignore[arg-type]
                validation_notes=validation_notes,
                derivation_source="semantic_llm",
                semantic_reason=_clean_optional_str(proposal.get("reason")),
                semantic_catalog_candidates=[candidate["candidate_id"]],
            )
        )

    return items, review


def _review_from_payload(payload: dict[str, Any], evidence: list[Evidence]) -> list[ReviewCandidate]:
    evidence_by_id = {item.evidence_id: item for item in evidence}
    result: list[ReviewCandidate] = []
    for item in _as_list(payload.get("review_candidates")):
        evidence_ids = _valid_evidence_ids(item.get("evidence_ids"), evidence_by_id)
        result.append(
            ReviewCandidate(
                evidence=str(item.get("evidence") or "LLM-Review-Kandidat"),
                evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                possible_gops=[str(gop) for gop in _as_list(item.get("possible_gops")) if str(gop).strip()],
                reason=str(item.get("reason") or "Semantisch unsicher; manuelle Pruefung erforderlich."),
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
                evidence=str(item.get("evidence") or "Nicht uebernommene Evidenz"),
                evidence_pages=_pages_for_ids(evidence_ids, evidence_by_id),
                not_billed_gop=_clean_optional_str(item.get("not_billed_gop")),
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


def _safe_quantity(value: Any) -> int:
    try:
        quantity = int(value)
    except (TypeError, ValueError):
        return 1
    return max(quantity, 1)
