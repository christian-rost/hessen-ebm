from __future__ import annotations

import json
from functools import lru_cache
from typing import Any, Iterable
from uuid import uuid4

from .billing_rule_definitions import (
    BillingRuleSet,
    billing_rule_set_payload,
    load_billing_rule_set,
    parse_billing_rule_set,
)
from .config import get_settings
from .clinical_definitions import (
    ClinicalDefinitionSet,
    clinical_definition_set_payload,
    load_clinical_definition_set,
    parse_clinical_definition_set,
)
from .database import get_supabase
from .ebm_rule_compiler import CompiledCatalogRuleSet, CompiledGopRule


RULE_SETS_TABLE = "hessen_ebm_rule_sets"
RULE_DEFINITIONS_TABLE = "hessen_ebm_rule_definitions"
RULE_CLAUSES_TABLE = "hessen_ebm_rule_clauses"
RULE_COMPILE_RUNS_TABLE = "hessen_ebm_rule_compile_runs"

_last_status: dict[str, Any] = {
    "source": "json",
    "supabase_configured": False,
    "last_error": None,
    "active_rule_set": None,
}


def rule_set_key(compiled: CompiledCatalogRuleSet) -> str:
    region = compiled.region.strip().casefold().replace(" ", "-")
    return f"{compiled.rule_set_id}:{compiled.version}:{region}"


def build_rule_set_row(
    compiled: CompiledCatalogRuleSet,
    core_rule_set: BillingRuleSet,
    clinical_definitions: ClinicalDefinitionSet | None = None,
    *,
    status: str = "publishing",
) -> dict[str, Any]:
    core_payload = billing_rule_set_payload(core_rule_set)
    core_payload["clinical_definitions"] = clinical_definition_set_payload(
        clinical_definitions or load_clinical_definition_set(site_path=get_settings().site_definitions_path)
    )
    return {
        "rule_set_key": rule_set_key(compiled),
        "rule_set_id": compiled.rule_set_id,
        "version": compiled.version,
        "schema_version": core_rule_set.schema_version,
        "quarter": compiled.quarter,
        "region": compiled.region,
        "status": status,
        "source_catalog_id": compiled.source_catalog_id,
        "source_data_stand": compiled.source_data_stand,
        "source_hash": compiled.source_hash,
        "compiled_at": compiled.compiled_at,
        "core_payload": core_payload,
        "summary": compiled.summary,
    }


def build_definition_rows(compiled: CompiledCatalogRuleSet) -> list[dict[str, Any]]:
    key = rule_set_key(compiled)
    rows: list[dict[str, Any]] = []
    for rule in compiled.rules:
        machine_count = sum(1 for clause in rule.clauses if clause.machine_executable)
        review_count = sum(1 for clause in rule.clauses if clause.review_required)
        rows.append(
            {
                "definition_key": _definition_key(key, rule),
                "rule_set_key": key,
                "rule_id": rule.rule_id,
                "definition_type": rule.definition_type,
                "source_type": rule.source_type,
                "source_catalog_id": rule.source_catalog_id,
                "quarter": rule.quarter,
                "region": rule.region,
                "catalog_key": rule.catalog_key,
                "gop": rule.gop,
                "gop_base": rule.gop_base,
                "title": rule.title,
                "valid_from": rule.quarter,
                "valid_to": rule.quarter,
                "coverage_status": rule.coverage_status,
                "machine_clause_count": machine_count,
                "review_clause_count": review_count,
                "source_text": rule.source_text,
                "source_reference": rule.source_reference,
                "scope": rule.scope,
                "definition": rule.definition_payload(),
            }
        )
    return rows


def build_clause_rows(compiled: CompiledCatalogRuleSet) -> list[dict[str, Any]]:
    key = rule_set_key(compiled)
    rows: list[dict[str, Any]] = []
    for rule in compiled.rules:
        definition_key = _definition_key(key, rule)
        for index, clause in enumerate(rule.clauses):
            rows.append(
                {
                    "clause_key": f"{definition_key}:{index:04d}",
                    "definition_key": definition_key,
                    "rule_set_key": key,
                    "rule_id": rule.rule_id,
                    "clause_index": index,
                    "clause_type": clause.clause_type,
                    "scope": clause.scope,
                    "parameters": clause.parameters,
                    "source_text": clause.source_text,
                    "machine_executable": clause.machine_executable,
                    "review_required": clause.review_required,
                    "confidence": clause.confidence,
                }
            )
    return rows


def publish_compiled_rule_set(
    compiled: CompiledCatalogRuleSet,
    core_rule_set: BillingRuleSet | None = None,
    clinical_definitions: ClinicalDefinitionSet | None = None,
) -> dict[str, Any]:
    client = get_supabase()
    if not client:
        raise RuntimeError("Supabase ist nicht konfiguriert; das Regelwerk kann nicht migriert werden.")
    core = core_rule_set or load_billing_rule_set(site_path=get_settings().site_definitions_path)
    key = rule_set_key(compiled)
    run_id = str(uuid4())
    client.table(RULE_COMPILE_RUNS_TABLE).insert(
        {
            "run_id": run_id,
            "quarter": compiled.quarter,
            "region": compiled.region,
            "rule_set_key": None,
            "status": "running",
            "summary": {},
        }
    ).execute()
    try:
        client.table(RULE_SETS_TABLE).upsert(
            build_rule_set_row(compiled, core, clinical_definitions)
        ).execute()
        client.table(RULE_DEFINITIONS_TABLE).delete().eq("rule_set_key", key).execute()
        definitions = build_definition_rows(compiled)
        clauses = build_clause_rows(compiled)
        _insert_batches(client, RULE_DEFINITIONS_TABLE, definitions, 150)
        _insert_batches(client, RULE_CLAUSES_TABLE, clauses, 300)
        (
            client.table(RULE_SETS_TABLE)
            .update({"status": "superseded"})
            .eq("quarter", compiled.quarter)
            .eq("region", compiled.region)
            .eq("status", "active")
            .neq("rule_set_key", key)
            .execute()
        )
        (
            client.table(RULE_SETS_TABLE)
            .update({"status": "active", "activated_at": compiled.compiled_at})
            .eq("rule_set_key", key)
            .execute()
        )
        (
            client.table(RULE_COMPILE_RUNS_TABLE)
            .update(
                {
                    "status": "succeeded",
                    "finished_at": compiled.compiled_at,
                    "rule_set_key": key,
                    "summary": compiled.summary,
                }
            )
            .eq("run_id", run_id)
            .execute()
        )
    except Exception as exc:
        try:
            client.table(RULE_SETS_TABLE).update({"status": "failed"}).eq("rule_set_key", key).execute()
            (
                client.table(RULE_COMPILE_RUNS_TABLE)
                .update({"status": "failed", "finished_at": compiled.compiled_at, "error": str(exc)[:2000]})
                .eq("run_id", run_id)
                .execute()
            )
        except Exception:
            pass
        raise
    clear_rule_store_cache()
    return {
        "migrated": True,
        "backend": "supabase",
        "rule_set_key": key,
        "run_id": run_id,
        "definitions": len(definitions),
        "clauses": len(clauses),
        "summary": compiled.summary,
    }


@lru_cache(maxsize=16)
def get_runtime_billing_rule_set(quarter: str | None = None, region: str = "Hessen") -> BillingRuleSet:
    settings = get_settings()
    local = load_billing_rule_set(site_path=get_settings().site_definitions_path)
    source = settings.billing_rules_source.strip().casefold()
    try:
        client = get_supabase() if source in {"auto", "supabase"} else None
    except Exception as exc:
        _last_status.update(
            {
                "source": "json",
                "supabase_configured": False,
                "last_error": str(exc),
                "active_rule_set": None,
                "fallback": "json",
            }
        )
        return local
    _last_status.update(
        {
            "source": "json",
            "supabase_configured": bool(client),
            "last_error": None,
            "active_rule_set": None,
            "fallback": None,
        }
    )
    if not client:
        return local
    try:
        row = _active_rule_set_row(client, quarter, region)
        if not row:
            return local
        payload = row.get("core_payload")
        if not isinstance(payload, dict):
            raise ValueError("Das aktive Supabase-Regelwerk enthält kein gültiges core_payload.")
        remote = parse_billing_rule_set(payload)
        result = _merge_runtime_core_rules(local, remote)
        _last_status.update(
            {
                "source": "supabase",
                "active_rule_set": {
                    "rule_set_key": row.get("rule_set_key"),
                    "quarter": row.get("quarter"),
                    "region": row.get("region"),
                    "version": row.get("version"),
                    "core_rule_version": result.version,
                    "compiled_at": row.get("compiled_at"),
                    "summary": row.get("summary") if isinstance(row.get("summary"), dict) else {},
                },
            }
        )
        return result
    except Exception as exc:
        _last_status["last_error"] = str(exc)
        if source == "supabase":
            _last_status["fallback"] = "json"
        return local


@lru_cache(maxsize=16)
def get_runtime_clinical_definition_set(
    quarter: str | None = None,
    region: str = "Hessen",
) -> ClinicalDefinitionSet:
    local = load_clinical_definition_set(site_path=get_settings().site_definitions_path)
    settings = get_settings()
    source = settings.billing_rules_source.strip().casefold()
    if source not in {"auto", "supabase"}:
        return local
    try:
        client = get_supabase()
        if not client:
            return local
        row = _active_rule_set_row(client, quarter, region)
        core_payload = row.get("core_payload") if row else None
        payload = core_payload.get("clinical_definitions") if isinstance(core_payload, dict) else None
        if not isinstance(payload, dict):
            return local
        remote = parse_clinical_definition_set(payload)
        if remote.version != local.version:
            _last_status["clinical_definitions_fallback"] = {
                "reason": "Versionsabweichung",
                "local_version": local.version,
                "supabase_version": remote.version,
            }
            return local
        _last_status.pop("clinical_definitions_fallback", None)
        return remote
    except Exception as exc:
        _last_status["clinical_definitions_error"] = str(exc)
        return local


@lru_cache(maxsize=32)
def load_compiled_catalog_rules(
    quarter: str,
    region: str,
    gop_bases: tuple[str, ...],
) -> list[dict[str, Any]]:
    settings = get_settings()
    if settings.billing_rules_source.strip().casefold() == "json":
        return []
    try:
        client = get_supabase()
    except Exception as exc:
        _last_status["last_error"] = str(exc)
        return []
    if not client or not gop_bases:
        return []
    try:
        active = _active_rule_set_row(client, quarter, region)
        if not active:
            return []
        response = (
            client.table(RULE_DEFINITIONS_TABLE)
            .select("rule_id,definition_type,gop,gop_base,title,source_type,coverage_status,scope,definition")
            .eq("rule_set_key", active["rule_set_key"])
            .in_("gop_base", list(gop_bases))
            .execute()
        )
        direct = list(response.data or [])
        context_response = (
            client.table(RULE_DEFINITIONS_TABLE)
            .select("rule_id,definition_type,gop,gop_base,title,source_type,coverage_status,scope,definition")
            .eq("rule_set_key", active["rule_set_key"])
            .eq("definition_type", "catalog_context")
            .execute()
        )
        contexts = [
            row for row in (context_response.data or [])
            if _scope_matches_any(row.get("scope"), gop_bases)
        ]
        return direct + contexts
    except Exception as exc:
        _last_status["last_error"] = str(exc)
        return []


def rule_store_status() -> dict[str, Any]:
    settings = get_settings()
    try:
        supabase_configured = bool(get_supabase())
    except Exception as exc:
        supabase_configured = False
        _last_status["last_error"] = str(exc)
    return {
        **_last_status,
        "configured_source": settings.billing_rules_source,
        "supabase_configured": supabase_configured,
    }


def clear_rule_store_cache() -> None:
    get_runtime_billing_rule_set.cache_clear()
    get_runtime_clinical_definition_set.cache_clear()
    load_compiled_catalog_rules.cache_clear()


def _merge_runtime_core_rules(local: BillingRuleSet, remote: BillingRuleSet) -> BillingRuleSet:
    def merge(remote_rules: tuple[Any, ...], local_rules: tuple[Any, ...]) -> tuple[Any, ...]:
        by_id = {rule.rule_id: rule for rule in remote_rules}
        return tuple(remote_rules) + tuple(rule for rule in local_rules if rule.rule_id not in by_id)

    def merge_settings(local_settings: dict[str, Any], remote_settings: dict[str, Any]) -> dict[str, Any]:
        result = dict(local_settings)
        for key, remote_value in remote_settings.items():
            local_value = result.get(key)
            if isinstance(local_value, dict) and isinstance(remote_value, dict):
                result[key] = {**local_value, **remote_value}
            else:
                result[key] = remote_value
        return result

    return BillingRuleSet(
        schema_version=remote.schema_version,
        rule_set_id=remote.rule_set_id,
        version=f"{remote.version}+core-{local.version}" if remote.version != local.version else remote.version,
        candidate_rules=merge(remote.candidate_rules, local.candidate_rules),
        temporal_rules=merge(remote.temporal_rules, local.temporal_rules),
        event_sequence_rules=merge(remote.event_sequence_rules, local.event_sequence_rules),
        derived_rules=merge(remote.derived_rules, local.derived_rules),
        clause_policy=merge_settings(local.clause_policy, remote.clause_policy),
        event_settings=merge_settings(local.event_settings, remote.event_settings),
        calendar_definitions=remote.calendar_definitions or local.calendar_definitions,
        semantic_policy=remote.semantic_policy or local.semantic_policy,
    )


def _active_rule_set_row(client: Any, quarter: str | None, region: str) -> dict[str, Any] | None:
    query = (
        client.table(RULE_SETS_TABLE)
        .select("rule_set_key,rule_set_id,version,quarter,region,core_payload,summary,compiled_at")
        .eq("status", "active")
        .eq("region", region)
    )
    if quarter:
        query = query.eq("quarter", quarter)
    response = query.order("compiled_at", desc=True).limit(1).execute()
    rows = response.data or []
    return dict(rows[0]) if rows else None


def _insert_batches(
    client: Any,
    table: str,
    rows: list[dict[str, Any]],
    size: int,
    max_payload_bytes: int = 750_000,
) -> None:
    batch: list[dict[str, Any]] = []
    payload_bytes = 0
    for row in rows:
        row_bytes = len(json.dumps(row, ensure_ascii=False, default=str).encode("utf-8"))
        if batch and (len(batch) >= size or payload_bytes + row_bytes > max_payload_bytes):
            client.table(table).insert(batch).execute()
            batch = []
            payload_bytes = 0
        batch.append(row)
        payload_bytes += row_bytes
    if batch:
        client.table(table).insert(batch).execute()


def _definition_key(rule_set: str, rule: CompiledGopRule) -> str:
    return f"{rule_set}:{rule.rule_id}"


def _scope_matches_any(scope: Any, gop_bases: tuple[str, ...]) -> bool:
    if not isinstance(scope, dict):
        return False
    kind = scope.get("kind")
    if kind == "gop_range":
        start = str(scope.get("start") or "")
        end = str(scope.get("end") or "")
        return any(start <= gop <= end for gop in gop_bases)
    if kind == "global":
        return False
    affected = {str(gop) for gop in scope.get("affected_gops", [])}
    return bool(affected.intersection(gop_bases))
