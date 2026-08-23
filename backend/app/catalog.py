from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from .models import CatalogEntry


GOP_RE = re.compile(r"^([0-9]{5})([A-Z0-9*]+)?$")


def normalize_gop(gop: str) -> tuple[str, str | None]:
    cleaned = gop.strip().upper().replace(" ", "")
    if cleaned.isdigit() and len(cleaned) == 4:
        cleaned = cleaned.zfill(5)
    match = GOP_RE.match(cleaned)
    if not match:
        return cleaned, None
    return match.group(1), match.group(2)


def canonical_gop(gop: str) -> str:
    base, suffix = normalize_gop(gop)
    return f"{base}{suffix or ''}"


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


def _row_gop(row: sqlite3.Row) -> str:
    """GOP einer Ergebniszeile. EBM-Zeilen fuehren `gop`, regionale `gop_code`."""
    keys = row.keys()
    for name in ("gop", "gop_code", "gop_base"):
        if name in keys and row[name]:
            return str(row[name])
    return ""


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


# Retrieval-Parameter. Rein sprachlich, kein Abrechnungswissen: der Katalog
# formuliert Legenden ("Übersichtsaufnahme der Brustorgane"), die klinische
# Dokumentation benutzt andere Worte ("Röntgen Thorax"). Ein Substring-LIKE
# findet solche Treffer nicht, eine tokenweise Volltextsuche schon.
FTS_TABLE = "search"
FTS_MIN_TOKEN_LENGTH = 3
FTS_STOPWORDS = frozenset(
    {
        "als", "am", "an", "auf", "aus", "bei", "das", "dem", "den", "der", "des", "die",
        "ein", "eine", "einer", "eines", "fuer", "für", "im", "in", "je", "mit", "nach",
        "oder", "sowie", "und", "von", "vom", "zum", "zur", "über",
    }
)
_FTS_TOKEN_RE = re.compile(r"[0-9A-Za-zÄÖÜäöüß]+")


def build_fts_query(query: str) -> str | None:
    """Freitext in eine FTS5-Abfrage übersetzen.

    Die Tokens werden ODER-verknüpft und als Präfix gesucht. Damit trifft
    "Röntgen Thorax 2 Ebenen" auch eine Legende, die nur "Ebenen" und
    "Brustorgane" enthält; bm25 sortiert die beste Überdeckung nach oben.
    Rückgabe `None`, wenn nichts Brauchbares übrig bleibt.
    """
    tokens = [
        token
        for token in _FTS_TOKEN_RE.findall(query or "")
        if len(token) >= FTS_MIN_TOKEN_LENGTH and token.casefold() not in FTS_STOPWORDS
    ]
    if not tokens:
        return None
    return " OR ".join(f'"{token}"*' for token in dict.fromkeys(tokens))


class CatalogRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._section_cache: dict[str, list[str]] = {}

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

    def latest_quarter(self) -> str | None:
        """Neuestes im aktiven Katalog vorhandenes Quartal.

        Dient als Rückfallebene, wenn sich aus der Evidenz kein Leistungsquartal
        ableiten lässt. So steht kein fest verdrahtetes Quartal im Code.
        """
        if not self.available:
            return None
        with self._connect() as conn:
            if "snapshots" not in self._tables(conn):
                return None
            row = conn.execute(
                "select quarter from snapshots order by quarter desc limit 1"
            ).fetchone()
        return str(row["quarter"]) if row and row["quarter"] else None

    def section_paths(self, quarter: str) -> dict[str, list[str]]:
        """Abschnittspfad je GOP fuer ein Quartal.

        Der EBM ist ein gegliedertes Normwerk: Der Abschnitt, in dem eine GOP steht,
        sagt, fuer welchen Versorgungszusammenhang sie gilt - eine Notfallpauschale
        steht unter "Versorgung im Notfall und im organisierten Notfalldienst", eine
        Betreuungsleistung unter "Mutterschaftsvorsorge". Diese Zuordnung liegt im
        Baum und wurde bisher nicht mitgefuehrt.

        Der Baum eines Quartals umfasst wenige tausend Knoten und wird einmal je
        Quartal aufgebaut, nicht je Abfrage.
        """
        cached = self._section_cache.get(quarter)
        if cached is not None:
            return cached
        paths: dict[str, list[str]] = {}
        if self.available:
            with self._connect() as conn:
                columns = self._columns(conn, "nodes") if "nodes" in self._tables(conn) else set()
                if {"row_key", "parent_row_key", "label"}.issubset(columns):
                    rows = {
                        row["row_key"]: row
                        for row in conn.execute(
                            "select row_key, parent_row_key, label, is_leaf from nodes where quarter = ?",
                            (quarter,),
                        )
                    }
                    for row in rows.values():
                        label = str(row["label"] or "")
                        gop = label.split(" ", 1)[0].strip().upper()
                        if not re.fullmatch(r"\d{5}[A-Z0-9*]*", gop):
                            continue
                        chain: list[str] = []
                        parent = row["parent_row_key"]
                        seen: set[str] = set()
                        while parent in rows and parent not in seen:
                            seen.add(parent)
                            chain.append(str(rows[parent]["label"] or ""))
                            parent = rows[parent]["parent_row_key"]
                        paths.setdefault(gop, list(reversed(chain)))
        self._section_cache[quarter] = paths
        return paths

    def section_path(self, gop: str, quarter: str) -> list[str]:
        paths = self.section_paths(quarter)
        canonical = canonical_gop(gop)
        base, _ = normalize_gop(canonical)
        return paths.get(canonical) or paths.get(base) or []

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
            section_path=self.section_path(_row_gop(row), quarter),
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
            section_path=self.section_path(_row_gop(row), quarter),
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

    def _fts_ranked_gops(self, conn: sqlite3.Connection, query: str, quarter: str, limit: int) -> list[str]:
        """GOPs des Quartals nach bm25-Relevanz, beste zuerst. Leer, wenn kein Index da ist."""
        if FTS_TABLE not in self._tables(conn):
            return []
        match = build_fts_query(query)
        if not match:
            return []
        try:
            rows = conn.execute(
                f"select gop, bm25({FTS_TABLE}) as score from {FTS_TABLE} "
                f"where {FTS_TABLE} match ? and quarter = ? order by score limit ?",
                (match, quarter, limit),
            ).fetchall()
        except sqlite3.OperationalError:
            # Beschaedigter oder inkompatibler Index darf die Suche nicht sprengen.
            return []
        return list(dict.fromkeys(str(row["gop"]) for row in rows if row["gop"]))

    def _fts_ebm_rows(
        self,
        conn: sqlite3.Connection,
        tables: set[str],
        detail_columns: set[str],
        query: str,
        quarter: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        """EBM-Detailzeilen in bm25-Reihenfolge; leer, wenn der Index nichts liefert."""
        ranked_gops = self._fts_ranked_gops(conn, query, quarter, limit)
        if not ranked_gops:
            return []
        detail_text_expr = "d.text" if "text" in detail_columns else "null"
        data_stand_expr = "s.data_stand" if "snapshots" in tables else "null as data_stand"
        join_expr = "left join snapshots s on s.quarter = d.quarter " if "snapshots" in tables else ""
        placeholders = ",".join("?" for _ in ranked_gops)
        fetched = {
            str(row["gop"]): row
            for row in conn.execute(
                f"select d.gop, d.title, d.points, d.euro, {data_stand_expr}, {detail_text_expr} as rule_text "
                f"from details d {join_expr}"
                f"where d.quarter = ? and d.gop in ({placeholders})",
                (quarter, *ranked_gops),
            )
        }
        # Exakte GOP-Eingabe bleibt vorne, sonst gilt die Relevanzreihenfolge.
        exact = query.strip().upper()
        ordered = sorted(ranked_gops, key=lambda gop: 0 if gop.upper() == exact else 1)
        return [fetched[gop] for gop in ordered if gop in fetched]

    def _like_ebm_rows(
        self,
        conn: sqlite3.Connection,
        tables: set[str],
        detail_columns: set[str],
        query: str,
        quarter: str,
        limit: int,
    ) -> list[sqlite3.Row]:
        term = f"%{query.strip()}%"
        detail_text_expr = "d.text" if "text" in detail_columns else "null"
        detail_text_filter = " or d.text like ?" if "text" in detail_columns else ""
        data_stand_expr = "s.data_stand" if "snapshots" in tables else "null as data_stand"
        join_expr = "left join snapshots s on s.quarter = d.quarter " if "snapshots" in tables else ""
        params: tuple[Any, ...]
        if "text" in detail_columns:
            params = (quarter, term, term, term, query.strip(), f"{query.strip()}%", limit)
        else:
            params = (quarter, term, term, query.strip(), f"{query.strip()}%", limit)
        return conn.execute(
            f"select d.gop, d.title, d.points, d.euro, {data_stand_expr}, {detail_text_expr} as rule_text "
            f"from details d {join_expr}"
            f"where d.quarter = ? and (d.gop like ? or d.title like ?{detail_text_filter}) "
            "order by case when d.gop = ? then 0 when d.gop like ? then 1 else 2 end, d.gop "
            "limit ?",
            params,
        ).fetchall()

    def search(self, query: str, quarter: str, limit: int = 25) -> list[CatalogEntry]:
        if not self.available:
            return []
        term = f"%{query.strip()}%"
        with self._connect() as conn:
            tables = self._tables(conn)
            detail_columns = self._columns(conn, "details") if "details" in tables else set()
            # Bevorzugt der Volltextindex, sonst das bisherige LIKE.
            ebm_rows = self._fts_ebm_rows(conn, tables, detail_columns, query, quarter, limit)
            if not ebm_rows:
                ebm_rows = self._like_ebm_rows(conn, tables, detail_columns, query, quarter, limit)
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
                    section_path=self.section_path(_row_gop(row), quarter),
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
                    section_path=self.section_path(_row_gop(row), quarter),
                )
            )
        return entries[:limit]
