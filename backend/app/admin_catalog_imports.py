from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .admin_catalog import CatalogValidationError, install_catalog_database, validate_catalog_database
from .ebm_kbv_scraper import scrape as scrape_ebm_kbv
from .hessen_gop_importer import catalog_exists, delete_existing_catalog, ensure_schema, import_pdf


class CatalogImportError(RuntimeError):
    pass


def import_regional_catalog_pdf(
    *,
    pdf_path: Path,
    target_path: Path,
    backup_dir: Path,
    work_dir: Path,
    catalog_id: str | None,
    source_system: str,
    region: str,
    quarter: str,
    replace: bool,
) -> dict[str, Any]:
    if not pdf_path.exists() or pdf_path.stat().st_size == 0:
        raise CatalogImportError("Das PDF des regionalen Katalogs fehlt oder ist leer.")
    if not target_path.exists():
        raise CatalogImportError("Vor dem Regionalimport wird eine aktive EBM-Katalogdatenbank benötigt.")

    quarter = _require_value(quarter, "quarter")
    region = _require_value(region, "region")
    source_system = _require_value(source_system, "source_system")
    effective_catalog_id = catalog_id or _default_regional_catalog_id(source_system, region, quarter)
    working_db = _prepare_working_database(target_path, work_dir, "regional")

    try:
        with sqlite3.connect(working_db) as conn:
            conn.execute("PRAGMA foreign_keys=ON")
            ensure_schema(conn)
            if replace:
                delete_existing_catalog(
                    conn,
                    catalog_id=effective_catalog_id,
                    source_system=source_system,
                    region=region,
                    quarter=quarter,
                )
            elif catalog_exists(
                conn,
                catalog_id=effective_catalog_id,
                source_system=source_system,
                region=region,
                quarter=quarter,
            ):
                raise CatalogImportError("Der regionale Katalog ist bereits vorhanden. Aktivieren Sie Ersetzen, um ihn zu aktualisieren.")
            result = import_pdf(
                conn,
                pdf_path=pdf_path,
                catalog_id=effective_catalog_id,
                source_system=source_system,
                region=region,
                quarter=quarter,
            )
            if int(result.get("regional_gops") or 0) == 0:
                raise CatalogImportError("Im hochgeladenen PDF wurden keine regionalen GOP-Einträge erkannt.")
            conn.commit()

        validation = validate_catalog_database(working_db)
        install_result = install_catalog_database(
            uploaded_path=working_db,
            target_path=target_path,
            backup_dir=backup_dir,
        )
        return {
            "imported": True,
            "kind": "regional_catalog",
            "catalog_id": effective_catalog_id,
            "source_file": str(pdf_path),
            "result": result,
            "validation": validation,
            "install": install_result,
        }
    except Exception:
        _cleanup_sqlite_family(working_db)
        raise


def scrape_ebm_quarter_into_catalog(
    *,
    target_path: Path,
    backup_dir: Path,
    work_dir: Path,
    quarter: str,
    replace_quarter: bool,
    delay: float,
    timeout: int,
    commit_every: int,
    progress_every: int,
) -> dict[str, Any]:
    quarter = _require_value(quarter, "quarter")
    working_db = _prepare_working_database(target_path, work_dir, "ebm-scrape")
    args = argparse.Namespace(
        db=str(working_db),
        quarter=quarter,
        delay=delay,
        timeout=timeout,
        reset=False,
        replace_quarter=replace_quarter,
        resume=not replace_quarter,
        no_details=False,
        limit_nodes=0,
        limit_details=0,
        commit_every=commit_every,
        progress_every=progress_every,
    )

    try:
        scrape_ebm_kbv(args)
        validation = validate_catalog_database(working_db)
        snapshot = _snapshot_for_quarter(working_db, quarter)
        if int(snapshot.get("detail_count") or 0) == 0:
            raise CatalogImportError(f"Der importierte EBM-Snapshot {quarter} enthält keine Detaildaten.")
        install_result = install_catalog_database(
            uploaded_path=working_db,
            target_path=target_path,
            backup_dir=backup_dir,
        )
        return {
            "imported": True,
            "kind": "ebm_snapshot",
            "quarter": quarter,
            "snapshot": snapshot,
            "validation": validation,
            "install": install_result,
        }
    except Exception:
        _cleanup_sqlite_family(working_db)
        raise


def _prepare_working_database(target_path: Path, work_dir: Path, prefix: str) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    working_db = work_dir / f"{prefix}-{timestamp}-{os.getpid()}.sqlite"
    if target_path.exists():
        shutil.copy2(target_path, working_db)
    return working_db


def _snapshot_for_quarter(db_path: Path, quarter: str) -> dict[str, Any]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            select quarter, source_url, site_version, data_stand, retrieved_at,
                   node_count, detail_count
            from snapshots
            where quarter = ?
            """,
            (quarter,),
        ).fetchone()
        if not row:
            raise CatalogValidationError(f"Der importierte EBM-Snapshot {quarter} wurde nach dem Scraping nicht gefunden.")
        return dict(row)


def _default_regional_catalog_id(source_system: str, region: str, quarter: str) -> str:
    normalized = f"{source_system}_{region}_{quarter}".lower()
    return "".join(char if char.isalnum() else "_" for char in normalized).strip("_")


def _require_value(value: str, field_name: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise CatalogImportError(f"Das Pflichtfeld {field_name} fehlt.")
    return cleaned


def _cleanup_sqlite_family(path: Path) -> None:
    for candidate in [path, Path(f"{path}-wal"), Path(f"{path}-shm")]:
        try:
            if candidate.exists():
                candidate.unlink()
        except OSError:
            pass
