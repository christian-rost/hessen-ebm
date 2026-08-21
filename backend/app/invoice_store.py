from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import get_supabase, supabase_status
from .models import AnalysisResult, BillingItem

INVOICES_TABLE = "hessen_ebm_invoices"
INVOICE_ITEMS_TABLE = "hessen_ebm_invoice_items"

INVOICE_LIST_COLUMNS = (
    "analysis_id,created_at,updated_at,source_filename,source_sha256,status,quarter,"
    "treatment_start,treatment_end,region,diagnosis,line_count,points_total,amount_total_eur,"
    "human_review_required,storage_backend"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _case_context(result: AnalysisResult) -> dict[str, Any]:
    value = result.catalog_context.get("case_context", {})
    return value if isinstance(value, dict) else {}


def _diagnosis(case_context: dict[str, Any]) -> str | None:
    value = case_context.get("diagnosis") or case_context.get("diagnoses")
    if isinstance(value, list):
        return ", ".join(str(item) for item in value if item)
    if value:
        return str(value)
    return None


def _sanitize_supabase_value(value: Any) -> Any:
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {
            (key.replace("\x00", "") if isinstance(key, str) else key): _sanitize_supabase_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize_supabase_value(item) for item in value]
    return value


def build_invoice_row(result: AnalysisResult) -> dict[str, Any]:
    case_context = _case_context(result)
    now = _now_iso()
    return _sanitize_supabase_value({
        "analysis_id": result.analysis_id,
        "updated_at": now,
        "source_filename": result.source_filename,
        "source_sha256": result.source_sha256,
        "status": result.status,
        "quarter": case_context.get("quarter"),
        "treatment_start": case_context.get("treatment_start"),
        "treatment_end": case_context.get("treatment_end"),
        "region": case_context.get("region") or "Hessen",
        "diagnosis": _diagnosis(case_context),
        "line_count": result.summary.line_count,
        "points_total": result.summary.points_total,
        "amount_total_eur": result.summary.amount_total_eur,
        "human_review_required": result.summary.human_review_required,
        "payload": result.model_dump(),
        "storage_backend": "supabase",
    })


def build_invoice_item_rows(result: AnalysisResult) -> list[dict[str, Any]]:
    return [_invoice_item_row(result.analysis_id, item) for item in result.items]


def _invoice_item_row(analysis_id: str, item: BillingItem) -> dict[str, Any]:
    return _sanitize_supabase_value({
        "analysis_id": analysis_id,
        "line": item.line,
        "gop_original": item.gop_original,
        "gop_base": item.gop_base,
        "gop_suffix": item.gop_suffix,
        "title": item.title,
        "catalog_source": item.catalog_source,
        "catalog_source_label": item.catalog_source_label,
        "catalog_id": item.catalog_id,
        "catalog_data_stand": item.catalog_data_stand,
        "quarter": item.quarter,
        "service_date": item.service_date,
        "service_time": item.service_time,
        "quantity": item.quantity,
        "points": item.points,
        "amount_eur": item.amount_eur,
        "rule_id": item.rule_id,
        "confidence": item.confidence,
        "evidence_ids": item.evidence_ids,
        "evidence_pages": item.evidence_pages,
        "validation_status": item.validation_status,
        "validation_notes": item.validation_notes,
        "derivation_source": item.derivation_source,
        "semantic_reason": item.semantic_reason,
        "semantic_catalog_candidates": item.semantic_catalog_candidates,
        "payload": item.model_dump(),
    })


def save_invoice(result: AnalysisResult) -> dict[str, object]:
    client = get_supabase()
    if not client:
        return {"stored": False, "backend": "local_json", "configured": False}

    row = build_invoice_row(result)
    client.table(INVOICES_TABLE).upsert(row).execute()
    client.table(INVOICE_ITEMS_TABLE).delete().eq("analysis_id", result.analysis_id).execute()
    item_rows = build_invoice_item_rows(result)
    if item_rows:
        client.table(INVOICE_ITEMS_TABLE).insert(item_rows).execute()
    return {"stored": True, "backend": "supabase", "configured": True}


def load_invoice(analysis_id: str) -> AnalysisResult | None:
    client = get_supabase()
    if not client:
        return None

    response = (
        client.table(INVOICES_TABLE)
        .select("payload")
        .eq("analysis_id", analysis_id)
        .limit(1)
        .execute()
    )
    rows = response.data or []
    if not rows:
        return None
    payload = rows[0].get("payload")
    if isinstance(payload, str):
        return AnalysisResult.model_validate_json(payload)
    return AnalysisResult.model_validate(payload)


def delete_invoice(analysis_id: str, analysis_dir: Path) -> dict[str, object]:
    client = get_supabase()
    deleted_supabase = False
    if client:
        response = (
            client.table(INVOICES_TABLE)
            .select("analysis_id")
            .eq("analysis_id", analysis_id)
            .limit(1)
            .execute()
        )
        deleted_supabase = bool(response.data or [])
        client.table(INVOICE_ITEMS_TABLE).delete().eq("analysis_id", analysis_id).execute()
        client.table(INVOICES_TABLE).delete().eq("analysis_id", analysis_id).execute()

    deleted_local = delete_local_invoice(analysis_id, analysis_dir)
    return {
        "deleted": deleted_supabase or deleted_local,
        "analysis_id": analysis_id,
        "deleted_supabase": deleted_supabase,
        "deleted_local_json": deleted_local,
        "storage_backend": "supabase" if client else "local_json",
    }


def delete_local_invoice(analysis_id: str, analysis_dir: Path) -> bool:
    path = analysis_dir / f"{analysis_id}.json"
    if not path.exists():
        return False
    path.unlink()
    return True


def list_invoices(limit: int, offset: int, analysis_dir: Path) -> dict[str, object]:
    client = get_supabase()
    if not client:
        local = list_local_invoices(analysis_dir, limit=limit, offset=offset)
        local["supabase"] = supabase_status()
        return local

    response = (
        client.table(INVOICES_TABLE)
        .select(INVOICE_LIST_COLUMNS)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
        .execute()
    )
    rows = response.data or []
    return {
        "items": [_normalize_invoice_summary(row) for row in rows],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "storage_backend": "supabase",
        "supabase": supabase_status(),
    }


def list_local_invoices(analysis_dir: Path, limit: int = 50, offset: int = 0) -> dict[str, object]:
    if not analysis_dir.exists():
        return {
            "items": [],
            "total": 0,
            "limit": limit,
            "offset": offset,
            "storage_backend": "local_json",
        }

    rows: list[dict[str, Any]] = []
    for path in analysis_dir.glob("*.json"):
        try:
            result = AnalysisResult.model_validate_json(path.read_text(encoding="utf-8"))
            row = build_invoice_row(result)
            created_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            row["created_at"] = created_at
            row["updated_at"] = created_at
            row["storage_backend"] = "local_json"
            row.pop("payload", None)
            rows.append(row)
        except (OSError, json.JSONDecodeError, ValueError):
            continue

    rows.sort(key=lambda row: str(row.get("created_at") or ""), reverse=True)
    paged = rows[offset : offset + limit]
    return {
        "items": [_normalize_invoice_summary(row) for row in paged],
        "total": len(rows),
        "limit": limit,
        "offset": offset,
        "storage_backend": "local_json",
    }


def _normalize_invoice_summary(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "analysis_id": row.get("analysis_id"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "source_filename": row.get("source_filename"),
        "source_sha256": row.get("source_sha256"),
        "status": row.get("status"),
        "quarter": row.get("quarter"),
        "treatment_start": row.get("treatment_start"),
        "treatment_end": row.get("treatment_end"),
        "region": row.get("region"),
        "diagnosis": row.get("diagnosis"),
        "line_count": int(row.get("line_count") or 0),
        "points_total": int(row.get("points_total") or 0),
        "amount_total_eur": float(row.get("amount_total_eur") or 0.0),
        "human_review_required": bool(row.get("human_review_required")),
        "storage_backend": row.get("storage_backend") or "supabase",
    }
