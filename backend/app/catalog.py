from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import CatalogEntry


GOP_RE = re.compile(r"^([0-9]{5})([A-Z0-9*]+)?$")


def normalize_gop(gop: str) -> tuple[str, str | None]:
    cleaned = gop.strip().upper().replace(" ", "")
    match = GOP_RE.match(cleaned)
    if not match:
        return cleaned, None
    return match.group(1), match.group(2)


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", ".").strip())
    except ValueError:
        return None


class CatalogRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path

    @property
    def available(self) -> bool:
        return self.db_path.exists() and self.db_path.is_file()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _tables(self, conn: sqlite3.Connection) -> set[str]:
        return {
            row["name"]
            for row in conn.execute(
                "select name from sqlite_master where type = 'table'"
            )
        }

    def status(self) -> dict[str, Any]:
        if not self.available:
            return {
                "available": False,
                "db_path": str(self.db_path),
                "snapshots": [],
                "regional_catalogs": [],
            }

        with self._connect() as conn:
            tables = self._tables(conn)
            snapshots = [
                dict(row)
                for row in conn.execute(
                    "select quarter, source_url, site_version, data_stand, retrieved_at, "
                    "node_count, detail_count from snapshots order by quarter"
                )
            ]
            regional_catalogs = []
            if "regional_catalogs" in tables:
                regional_catalogs = [
                    dict(row)
                    for row in conn.execute(
                        "select catalog_id, source_system, region, quarter, title, data_stand, "
                        "page_count from regional_catalogs order by quarter, region"
                    )
                ]
        return {
            "available": True,
            "db_path": str(self.db_path),
            "snapshots": snapshots,
            "regional_catalogs": regional_catalogs,
        }

    def lookup_ebm(self, gop: str, quarter: str) -> CatalogEntry | None:
        if not self.available:
            return None
        gop_base, _ = normalize_gop(gop)
        with self._connect() as conn:
            if "details" not in self._tables(conn):
                return None
            row = conn.execute(
                "select gop, title, points, euro from details where quarter = ? and gop = ?",
                (quarter, gop_base),
            ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            source="EBM_KBV",
            quarter=quarter,
            gop=row["gop"],
            gop_base=gop_base,
            title=row["title"] or gop_base,
            points=_to_int(row["points"]),
            euro=_to_float(row["euro"]),
        )

    def lookup_hessen(self, gop: str, quarter: str, region: str = "Hessen") -> CatalogEntry | None:
        if not self.available:
            return None
        gop_base, _ = normalize_gop(gop)
        with self._connect() as conn:
            if "regional_gops" not in self._tables(conn):
                return None
            row = conn.execute(
                "select gop_code, gop_base, title, points, euro, page from regional_gops "
                "where quarter = ? and region = ? and gop_base = ? "
                "order by gop_code limit 1",
                (quarter, region, gop_base),
            ).fetchone()
        if not row:
            return None
        return CatalogEntry(
            source="KV_HESSEN_GOP",
            quarter=quarter,
            region=region,
            gop=row["gop_code"],
            gop_base=row["gop_base"],
            title=row["title"] or row["gop_code"],
            points=_to_int(row["points"]),
            euro=_to_float(row["euro"]),
            page=_to_int(row["page"]),
        )

    def lookup(self, gop: str, quarter: str, region: str = "Hessen") -> CatalogEntry | None:
        return self.lookup_ebm(gop, quarter) or self.lookup_hessen(gop, quarter, region)

    def search(self, query: str, quarter: str, limit: int = 25) -> list[CatalogEntry]:
        if not self.available:
            return []
        term = f"%{query.strip()}%"
        with self._connect() as conn:
            tables = self._tables(conn)
            ebm_rows = conn.execute(
                "select gop, title, points, euro from details "
                "where quarter = ? and (gop like ? or title like ? or text like ?) "
                "order by case when gop = ? then 0 when gop like ? then 1 else 2 end, gop "
                "limit ?",
                (quarter, term, term, term, query.strip(), f"{query.strip()}%", limit),
            ).fetchall()
            regional_rows = []
            if "regional_gops" in tables:
                regional_rows = conn.execute(
                    "select gop_code, gop_base, title, points, euro, region, page from regional_gops "
                    "where quarter = ? and (gop_code like ? or title like ? or description like ?) "
                    "order by gop_code limit ?",
                    (quarter, term, term, term, limit),
                ).fetchall()

        entries: list[CatalogEntry] = []
        for row in ebm_rows:
            entries.append(
                CatalogEntry(
                    source="EBM_KBV",
                    quarter=quarter,
                    gop=row["gop"],
                    gop_base=row["gop"],
                    title=row["title"] or row["gop"],
                    points=_to_int(row["points"]),
                    euro=_to_float(row["euro"]),
                )
            )
        for row in regional_rows:
            entries.append(
                CatalogEntry(
                    source="KV_HESSEN_GOP",
                    quarter=quarter,
                    gop=row["gop_code"],
                    gop_base=row["gop_base"],
                    title=row["title"] or row["gop_code"],
                    points=_to_int(row["points"]),
                    euro=_to_float(row["euro"]),
                    region=row["region"],
                    page=_to_int(row["page"]),
                )
            )
        return entries[:limit]
