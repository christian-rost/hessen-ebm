from __future__ import annotations

import os
import shutil
import sqlite3
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUIRED_TABLES = {"snapshots", "nodes", "details"}


class CatalogValidationError(ValueError):
    pass


def validate_catalog_database(path: Path) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        raise CatalogValidationError("Uploaded catalog database file does not exist.")
    if path.stat().st_size == 0:
        raise CatalogValidationError("Uploaded catalog database file is empty.")

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise CatalogValidationError(f"Uploaded file is not a readable SQLite database: {exc}") from exc

    try:
        integrity = conn.execute("pragma integrity_check").fetchone()[0]
        if integrity != "ok":
            raise CatalogValidationError(f"SQLite integrity_check failed: {integrity}")

        tables = {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }
        missing = sorted(REQUIRED_TABLES - tables)
        if missing:
            raise CatalogValidationError(f"Catalog database is missing required tables: {', '.join(missing)}")

        snapshot_count = _count(conn, "snapshots")
        node_count = _count(conn, "nodes")
        detail_count = _count(conn, "details")
        if snapshot_count == 0 or detail_count == 0:
            raise CatalogValidationError("Catalog database contains no usable EBM snapshots/details.")

        snapshots = [
            dict(row)
            for row in conn.execute(
                "select quarter, source_url, site_version, data_stand, retrieved_at, "
                "node_count, detail_count from snapshots order by quarter"
            )
        ]

        regional_catalogs: list[dict[str, Any]] = []
        regional_gop_count = 0
        if "regional_catalogs" in tables:
            regional_catalogs = [
                dict(row)
                for row in conn.execute(
                    "select catalog_id, source_system, region, quarter, title, data_stand, "
                    "page_count from regional_catalogs order by quarter, region"
                )
            ]
        if "regional_gops" in tables:
            regional_gop_count = _count(conn, "regional_gops")
    finally:
        conn.close()

    return {
        "valid": True,
        "path": str(path),
        "size_bytes": path.stat().st_size,
        "sha256": _sha256_file(path),
        "tables": sorted(tables),
        "counts": {
            "snapshots": snapshot_count,
            "nodes": node_count,
            "details": detail_count,
            "regional_catalogs": len(regional_catalogs),
            "regional_gops": regional_gop_count,
        },
        "snapshots": snapshots,
        "regional_catalogs": regional_catalogs,
    }


def install_catalog_database(uploaded_path: Path, target_path: Path, backup_dir: Path) -> dict[str, Any]:
    validation = validate_catalog_database(uploaded_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    backup_dir.mkdir(parents=True, exist_ok=True)

    backup_path: Path | None = None
    if target_path.exists():
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        current_hash = _sha256_file(target_path)[:12]
        backup_path = backup_dir / f"{target_path.stem}-{timestamp}-{current_hash}{target_path.suffix}"
        shutil.copy2(target_path, backup_path)

    tmp_target = target_path.with_name(f".{target_path.name}.tmp-{os.getpid()}")
    shutil.copy2(uploaded_path, tmp_target)
    os.replace(tmp_target, target_path)
    _remove_sqlite_sidecars(target_path)

    installed_validation = validate_catalog_database(target_path)
    return {
        "installed": True,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "target_path": str(target_path),
        "backup_path": str(backup_path) if backup_path else None,
        "uploaded": validation,
        "active": installed_validation,
    }


def list_catalog_backups(backup_dir: Path) -> list[dict[str, Any]]:
    if not backup_dir.exists():
        return []
    backups = []
    for path in sorted(backup_dir.glob("*.sqlite"), reverse=True):
        stat = path.stat()
        backups.append(
            {
                "file": path.name,
                "path": str(path),
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            }
        )
    return backups


def _count(conn: sqlite3.Connection, table_name: str) -> int:
    row = conn.execute(f"select count(*) from {table_name}").fetchone()
    return int(row[0] if row else 0)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{path}{suffix}")
        if sidecar.exists():
            sidecar.unlink()
