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


def _clean_rule_texts(*values: Any) -> list[str]:
    texts: list[str] = []
    for value in values:
        if value is None:
            continue
        text = re.sub(r"\s+", " ", str(value)).strip()
        if text:
            texts.append(text)
    return list(dict.fromkeys(texts))


def _ebm_catalog_id(quarter: str) -> str:
    return f"ebm_kbv_{quarter.lower().replace('/', '_')}"


def _ebm_catalog_label(quarter: str) -> str:
    return f"KBV EBM {quarter}"


def _regional_catalog_label(source_system: str | None, region: str | None, quarter: str) -> str:
    parts = [part for part in (source_system, region, quarter) if part]
    return " ".join(parts) if parts else f"Regionaler Katalog {quarter}"


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

    def _columns(self, conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"pragma table_info({table})")}

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
            tables = self._tables(conn)
            if "details" not in tables:
                return None
            detail_columns = self._columns(conn, "details")
            rule_text_expr = "d.text" if "text" in detail_columns else "null"
            if "snapshots" in tables:
                row = conn.execute(
                    f"select d.gop, d.title, d.points, d.euro, s.data_stand, {rule_text_expr} as rule_text "
                    "from details d left join snapshots s on s.quarter = d.quarter "
                    "where d.quarter = ? and d.gop = ?",
                    (quarter, gop_base),
                ).fetchone()
            else:
                row = conn.execute(
                    f"select d.gop, d.title, d.points, d.euro, null as data_stand, {rule_text_expr} as rule_text "
                    "from details d where d.quarter = ? and d.gop = ?",
                    (quarter, gop_base),
                ).fetchone()
        if not row:
            return None
        rule_texts = _clean_rule_texts(row["rule_text"])
        return CatalogEntry(
            source="EBM_KBV",
            quarter=quarter,
            catalog_id=_ebm_catalog_id(quarter),
            catalog_label=_ebm_catalog_label(quarter),
            data_stand=row["data_stand"],
            gop=row["gop"],
            gop_base=gop_base,
            title=row["title"] or gop_base,
            points=_to_int(row["points"]),
            euro=_to_float(row["euro"]),
            description=rule_texts[0] if rule_texts else None,
            rule_texts=rule_texts,
        )

    def lookup_hessen(self, gop: str, quarter: str, region: str = "Hessen") -> CatalogEntry | None:
        if not self.available:
            return None
        gop_base, _ = normalize_gop(gop)
        rules: list[str] = []
        with self._connect() as conn:
            tables = self._tables(conn)
            if "regional_gops" not in tables:
                return None
            regional_columns = self._columns(conn, "regional_gops")
            description_expr = "g.description" if "description" in regional_columns else "null"
            if "regional_catalogs" in tables:
                row = conn.execute(
                    "select g.catalog_id, g.gop_code, g.gop_base, g.title, g.points, g.euro, g.page, "
                    f"{description_expr} as description, c.source_system, c.region, c.quarter, c.data_stand "
                    "from regional_gops g "
                    "join regional_catalogs c on c.catalog_id = g.catalog_id "
                    "where g.quarter = ? and g.region = ? and g.gop_base = ? "
                    "order by g.gop_code limit 1",
                    (quarter, region, gop_base),
                ).fetchone()
            else:
                row = conn.execute(
                    "select null as catalog_id, gop_code, gop_base, title, points, euro, page, "
                    f"{description_expr} as description, source_system, region, quarter, null as data_stand "
                    "from regional_gops g where quarter = ? and region = ? and gop_base = ? "
                    "order by gop_code limit 1",
                    (quarter, region, gop_base),
                ).fetchone()
            if row and "regional_gop_rules" in tables:
                rules = [
                    rule_row["rule_text"]
                    for rule_row in conn.execute(
                        "select rule_text from regional_gop_rules "
                        "where quarter = ? and region = ? and (gop_code = ? or gop_code = ? or gop_code is null) "
                        "order by id limit 20",
                        (quarter, region, row["gop_code"], row["gop_base"]),
                    )
                    if rule_row["rule_text"]
                ]
        if not row:
            return None
        rule_texts = _clean_rule_texts(row["description"], *rules)
        return CatalogEntry(
            source="KV_HESSEN_GOP",
            quarter=quarter,
            region=region,
            catalog_id=row["catalog_id"],
            catalog_label=_regional_catalog_label(row["source_system"], row["region"], row["quarter"]),
            data_stand=row["data_stand"],
            gop=row["gop_code"],
            gop_base=row["gop_base"],
            title=row["title"] or row["gop_code"],
            points=_to_int(row["points"]),
            euro=_to_float(row["euro"]),
            page=_to_int(row["page"]),
            description=row["description"],
            rule_texts=rule_texts,
        )

    def lookup(self, gop: str, quarter: str, region: str = "Hessen") -> CatalogEntry | None:
        return self.lookup_ebm(gop, quarter) or self.lookup_hessen(gop, quarter, region)

    def regional_catalog_check(self, gop_bases: list[str], quarter: str, region: str = "Hessen") -> dict[str, Any]:
        normalized_base_set: set[str] = set()
        for gop in gop_bases:
            gop_base, _ = normalize_gop(gop)
            if re.fullmatch(r"\d{5}", gop_base):
                normalized_base_set.add(gop_base)
        normalized_bases = sorted(normalized_base_set)
        result: dict[str, Any] = {
            "checked": False,
            "quarter": quarter,
            "region": region,
            "catalogs": [],
            "matched_gops": [],
            "matched_gop_bases": [],
            "missing_gop_bases": normalized_bases,
            "message": "",
        }
        if not self.available:
            result["message"] = "Regionalkatalog nicht geprüft: keine aktive Katalogdatenbank."
            return result

        with self._connect() as conn:
            tables = self._tables(conn)
            if "regional_gops" not in tables:
                result["message"] = "Regionalkatalog nicht geprüft: keine Regionaltabellen in der aktiven Datenbank."
                return result

            catalogs = self._regional_catalogs_for(conn, tables, quarter, region)
            result["catalogs"] = catalogs
            if not catalogs:
                result["message"] = f"Kein Regionalkatalog für {region} {quarter} in der aktiven Datenbank."
                return result

            result["checked"] = True
            if not normalized_bases:
                result["message"] = f"Regionalkatalog geprüft: {self._catalog_labels(catalogs)} ist aktiv."
                return result

            matches = self._regional_matches_for(conn, tables, normalized_bases, quarter, region)

        matched_bases = sorted({str(row["gop_base"]) for row in matches})
        result["matched_gops"] = matches
        result["matched_gop_bases"] = matched_bases
        result["missing_gop_bases"] = [base for base in normalized_bases if base not in matched_bases]
        if matches:
            result["message"] = (
                f"Regionalkatalog geprüft: {self._catalog_labels(catalogs)} enthält regionale Treffer "
                f"für {', '.join(matched_bases)}."
            )
        else:
            result["message"] = (
                f"Regionalkatalog geprüft: {self._catalog_labels(catalogs)} enthält keine passenden "
                f"regionalen GOPs zu den übernommenen Positionen ({', '.join(normalized_bases)})."
            )
        return result

    def _regional_catalogs_for(
        self,
        conn: sqlite3.Connection,
        tables: set[str],
        quarter: str,
        region: str,
    ) -> list[dict[str, Any]]:
        if "regional_catalogs" in tables:
            return [
                dict(row)
                for row in conn.execute(
                    "select catalog_id, source_system, region, quarter, title, data_stand, page_count "
                    "from regional_catalogs where quarter = ? and region = ? order by catalog_id",
                    (quarter, region),
                )
            ]
        return [
            {
                "catalog_id": None,
                "source_system": row["source_system"],
                "region": row["region"],
                "quarter": row["quarter"],
                "title": None,
                "data_stand": None,
                "page_count": None,
            }
            for row in conn.execute(
                "select distinct source_system, region, quarter from regional_gops "
                "where quarter = ? and region = ? order by source_system",
                (quarter, region),
            )
        ]

    def _regional_matches_for(
        self,
        conn: sqlite3.Connection,
        tables: set[str],
        gop_bases: list[str],
        quarter: str,
        region: str,
    ) -> list[dict[str, Any]]:
        placeholders = ", ".join("?" for _ in gop_bases)
        params: list[Any] = [quarter, region, *gop_bases]
        if "regional_catalogs" in tables:
            rows = conn.execute(
                "select g.catalog_id, g.gop_code, g.gop_base, g.title, g.points, g.euro, g.page, "
                "c.source_system, c.region, c.quarter, c.data_stand "
                "from regional_gops g "
                "join regional_catalogs c on c.catalog_id = g.catalog_id "
                f"where g.quarter = ? and g.region = ? and g.gop_base in ({placeholders}) "
                "order by g.gop_base, g.gop_code limit 50",
                params,
            ).fetchall()
        else:
            rows = conn.execute(
                "select null as catalog_id, gop_code, gop_base, title, points, euro, page, "
                "source_system, region, quarter, null as data_stand "
                "from regional_gops "
                f"where quarter = ? and region = ? and gop_base in ({placeholders}) "
                "order by gop_base, gop_code limit 50",
                params,
            ).fetchall()
        return [
            {
                "catalog_id": row["catalog_id"],
                "catalog_label": _regional_catalog_label(row["source_system"], row["region"], row["quarter"]),
                "source": "KV_HESSEN_GOP",
                "gop": row["gop_code"],
                "gop_base": row["gop_base"],
                "title": row["title"] or row["gop_code"],
                "points": _to_int(row["points"]),
                "euro": _to_float(row["euro"]),
                "page": _to_int(row["page"]),
                "data_stand": row["data_stand"],
            }
            for row in rows
        ]

    def _catalog_labels(self, catalogs: list[dict[str, Any]]) -> str:
        labels = [
            _regional_catalog_label(catalog.get("source_system"), catalog.get("region"), catalog.get("quarter") or "")
            for catalog in catalogs
        ]
        return ", ".join(labels)

    def search(self, query: str, quarter: str, limit: int = 25) -> list[CatalogEntry]:
        if not self.available:
            return []
        term = f"%{query.strip()}%"
        with self._connect() as conn:
            tables = self._tables(conn)
            detail_columns = self._columns(conn, "details") if "details" in tables else set()
            detail_text_expr = "d.text" if "text" in detail_columns else "null"
            detail_text_filter = " or d.text like ?" if "text" in detail_columns else ""
            if "snapshots" in tables:
                params: tuple[Any, ...]
                if "text" in detail_columns:
                    params = (quarter, term, term, term, query.strip(), f"{query.strip()}%", limit)
                else:
                    params = (quarter, term, term, query.strip(), f"{query.strip()}%", limit)
                ebm_rows = conn.execute(
                    f"select d.gop, d.title, d.points, d.euro, s.data_stand, {detail_text_expr} as rule_text "
                    "from details d left join snapshots s on s.quarter = d.quarter "
                    f"where d.quarter = ? and (d.gop like ? or d.title like ?{detail_text_filter}) "
                    "order by case when d.gop = ? then 0 when d.gop like ? then 1 else 2 end, d.gop "
                    "limit ?",
                    params,
                ).fetchall()
            else:
                if "text" in detail_columns:
                    params = (quarter, term, term, term, query.strip(), f"{query.strip()}%", limit)
                else:
                    params = (quarter, term, term, query.strip(), f"{query.strip()}%", limit)
                ebm_rows = conn.execute(
                    f"select d.gop, d.title, d.points, d.euro, null as data_stand, {detail_text_expr} as rule_text "
                    "from details d "
                    f"where d.quarter = ? and (d.gop like ? or d.title like ?{detail_text_filter}) "
                    "order by case when d.gop = ? then 0 when d.gop like ? then 1 else 2 end, d.gop "
                    "limit ?",
                    params,
                ).fetchall()
            regional_rows = []
            if "regional_gops" in tables:
                regional_columns = self._columns(conn, "regional_gops")
                regional_description_expr = "g.description" if "description" in regional_columns else "null"
                regional_description_filter = " or g.description like ?" if "description" in regional_columns else ""
                if "regional_catalogs" in tables:
                    if "description" in regional_columns:
                        params = (quarter, term, term, term, limit)
                    else:
                        params = (quarter, term, term, limit)
                    regional_rows = conn.execute(
                        "select g.catalog_id, g.gop_code, g.gop_base, g.title, g.points, g.euro, "
                        f"g.region, g.page, {regional_description_expr} as description, c.source_system, c.data_stand "
                        "from regional_gops g "
                        "join regional_catalogs c on c.catalog_id = g.catalog_id "
                        f"where g.quarter = ? and (g.gop_code like ? or g.title like ?{regional_description_filter}) "
                        "order by g.gop_code limit ?",
                        params,
                    ).fetchall()
                else:
                    if "description" in regional_columns:
                        params = (quarter, term, term, term, limit)
                    else:
                        params = (quarter, term, term, limit)
                    regional_rows = conn.execute(
                        "select null as catalog_id, gop_code, gop_base, title, points, euro, "
                        f"region, page, {regional_description_expr} as description, source_system, null as data_stand "
                        "from regional_gops g "
                        f"where quarter = ? and (gop_code like ? or title like ?{regional_description_filter}) "
                        "order by gop_code limit ?",
                        params,
                    ).fetchall()

        entries: list[CatalogEntry] = []
        for row in ebm_rows:
            rule_texts = _clean_rule_texts(row["rule_text"])
            entries.append(
                CatalogEntry(
                    source="EBM_KBV",
                    quarter=quarter,
                    catalog_id=_ebm_catalog_id(quarter),
                    catalog_label=_ebm_catalog_label(quarter),
                    data_stand=row["data_stand"],
                    gop=row["gop"],
                    gop_base=row["gop"],
                    title=row["title"] or row["gop"],
                    points=_to_int(row["points"]),
                    euro=_to_float(row["euro"]),
                    description=rule_texts[0] if rule_texts else None,
                    rule_texts=rule_texts,
                )
            )
        for row in regional_rows:
            rule_texts = _clean_rule_texts(row["description"])
            entries.append(
                CatalogEntry(
                    source="KV_HESSEN_GOP",
                    quarter=quarter,
                    catalog_id=row["catalog_id"],
                    catalog_label=_regional_catalog_label(row["source_system"], row["region"], quarter),
                    data_stand=row["data_stand"],
                    gop=row["gop_code"],
                    gop_base=row["gop_base"],
                    title=row["title"] or row["gop_code"],
                    points=_to_int(row["points"]),
                    euro=_to_float(row["euro"]),
                    region=row["region"],
                    page=_to_int(row["page"]),
                    description=row["description"],
                    rule_texts=rule_texts,
                )
            )
        return entries[:limit]
