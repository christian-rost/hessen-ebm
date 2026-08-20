from __future__ import annotations

from pathlib import Path
from typing import Any

from .billing_rule_definitions import load_billing_rule_set
from .billing_rule_store import publish_compiled_rule_set
from .ebm_rule_compiler import compile_catalog_quarter


def compile_and_migrate_catalog_rules(
    *,
    catalog_db_path: Path,
    quarter: str,
    region: str = "Hessen",
) -> dict[str, Any]:
    compiled = compile_catalog_quarter(
        catalog_db_path,
        quarter.strip(),
        region.strip() or "Hessen",
        include_regional=True,
    )
    migration = publish_compiled_rule_set(compiled, load_billing_rule_set())
    return {
        "compiled": True,
        "quarter": compiled.quarter,
        "region": compiled.region,
        "version": compiled.version,
        "source_hash": compiled.source_hash,
        "summary": compiled.summary,
        "supabase": migration,
    }
