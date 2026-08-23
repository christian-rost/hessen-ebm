#!/usr/bin/env python3
"""Import a KV Hessen GOP PDF into regional catalog tables.

The imported tables deliberately live next to the KBV EBM tables. Regional
GOPs have a different source, regional scope, suffix rules, payer constraints,
and contract context, so mixing them into the KBV `details` table would make
later billing validation ambiguous.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Optional

import pdfplumber


DEFAULT_SOURCE_SYSTEM = "KV_HESSEN_GOP"
DEFAULT_REGION = "Hessen"
DEFAULT_CATALOG_ID = "kv_hessen_gop_2025_q4"
BULLET = "\uf0a7"
EURO = "\u20ac"


@dataclass(frozen=True)
class GopVariant:
    original: str
    code: str
    base: str
    suffix: str
    markers: str
    footnotes: str


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="ebm_kbv.sqlite", help="SQLite database path")
    parser.add_argument("--pdf", required=True, help="KV Hessen GOP PDF path")
    parser.add_argument("--catalog-id", default=DEFAULT_CATALOG_ID)
    parser.add_argument("--source-system", default=DEFAULT_SOURCE_SYSTEM)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--quarter", required=True, help="Leistungsquartal des PDF-Katalogs, z. B. 2026/Q3")
    parser.add_argument("--replace", action="store_true", help="Replace an existing matching catalog")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    pdf_path = Path(args.pdf)
    if not pdf_path.exists():
        raise FileNotFoundError(pdf_path)

    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        ensure_schema(conn)
        if args.replace:
            delete_existing_catalog(
                conn,
                catalog_id=args.catalog_id,
                source_system=args.source_system,
                region=args.region,
                quarter=args.quarter,
            )
        elif catalog_exists(
            conn,
            catalog_id=args.catalog_id,
            source_system=args.source_system,
            region=args.region,
            quarter=args.quarter,
        ):
            raise RuntimeError(
                "Catalog already exists. Re-run with --replace to refresh it."
            )

        result = import_pdf(
            conn,
            pdf_path=pdf_path,
            catalog_id=args.catalog_id,
            source_system=args.source_system,
            region=args.region,
            quarter=args.quarter,
        )
        conn.commit()

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS regional_catalogs (
            catalog_id TEXT PRIMARY KEY,
            source_system TEXT NOT NULL,
            region TEXT NOT NULL,
            quarter TEXT NOT NULL,
            title TEXT,
            source_file TEXT,
            source_url TEXT,
            data_stand TEXT,
            imported_at TEXT NOT NULL,
            page_count INTEGER NOT NULL DEFAULT 0,
            sha256 TEXT,
            UNIQUE (source_system, region, quarter)
        );

        CREATE TABLE IF NOT EXISTS regional_gop_pages (
            catalog_id TEXT NOT NULL,
            page INTEGER NOT NULL,
            text TEXT,
            PRIMARY KEY (catalog_id, page),
            FOREIGN KEY (catalog_id)
                REFERENCES regional_catalogs(catalog_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS regional_gop_tables (
            catalog_id TEXT NOT NULL,
            page INTEGER NOT NULL,
            table_index INTEGER NOT NULL,
            row_index INTEGER NOT NULL,
            header_json TEXT,
            cells_json TEXT NOT NULL,
            PRIMARY KEY (catalog_id, page, table_index, row_index),
            FOREIGN KEY (catalog_id)
                REFERENCES regional_catalogs(catalog_id)
                ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS regional_gops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_id TEXT NOT NULL,
            source_system TEXT NOT NULL,
            region TEXT NOT NULL,
            quarter TEXT NOT NULL,
            gop_original TEXT NOT NULL,
            gop_code TEXT NOT NULL,
            gop_base TEXT NOT NULL,
            gop_suffix TEXT,
            markers TEXT,
            footnotes TEXT,
            section TEXT,
            title TEXT,
            description TEXT,
            valuation_text TEXT,
            euro REAL,
            points TEXT,
            unit TEXT,
            role TEXT,
            page INTEGER,
            table_index INTEGER,
            row_index INTEGER,
            raw_row_json TEXT,
            FOREIGN KEY (catalog_id)
                REFERENCES regional_catalogs(catalog_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_regional_gops_lookup
            ON regional_gops(quarter, region, gop_code);

        CREATE INDEX IF NOT EXISTS idx_regional_gops_base
            ON regional_gops(quarter, region, gop_base);

        CREATE TABLE IF NOT EXISTS regional_gop_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_id TEXT NOT NULL,
            gop_id INTEGER,
            quarter TEXT NOT NULL,
            region TEXT NOT NULL,
            gop_code TEXT,
            rule_type TEXT NOT NULL,
            rule_text TEXT NOT NULL,
            source_text TEXT,
            FOREIGN KEY (catalog_id)
                REFERENCES regional_catalogs(catalog_id)
                ON DELETE CASCADE,
            FOREIGN KEY (gop_id)
                REFERENCES regional_gops(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_regional_gop_rules_gop
            ON regional_gop_rules(quarter, region, gop_code);

        CREATE TABLE IF NOT EXISTS regional_gop_edges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_id TEXT NOT NULL,
            source_gop_id INTEGER,
            source_gop_code TEXT,
            target_gop_code TEXT NOT NULL,
            relation_type TEXT NOT NULL,
            context TEXT,
            source_text TEXT,
            FOREIGN KEY (catalog_id)
                REFERENCES regional_catalogs(catalog_id)
                ON DELETE CASCADE,
            FOREIGN KEY (source_gop_id)
                REFERENCES regional_gops(id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_regional_gop_edges_source
            ON regional_gop_edges(catalog_id, source_gop_code);

        CREATE INDEX IF NOT EXISTS idx_regional_gop_edges_target
            ON regional_gop_edges(catalog_id, target_gop_code);

        CREATE TABLE IF NOT EXISTS regional_gop_payers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_id TEXT NOT NULL,
            page INTEGER,
            context TEXT,
            payer_name TEXT NOT NULL,
            vknr TEXT NOT NULL,
            FOREIGN KEY (catalog_id)
                REFERENCES regional_catalogs(catalog_id)
                ON DELETE CASCADE,
            UNIQUE (catalog_id, payer_name, vknr, context)
        );
        """
    )


def catalog_exists(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    source_system: str,
    region: str,
    quarter: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1
        FROM regional_catalogs
        WHERE catalog_id = ?
           OR (source_system = ? AND region = ? AND quarter = ?)
        LIMIT 1
        """,
        (catalog_id, source_system, region, quarter),
    ).fetchone()
    return row is not None


def delete_existing_catalog(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    source_system: str,
    region: str,
    quarter: str,
) -> None:
    catalog_ids = {
        catalog_id,
        *(
            row[0]
            for row in conn.execute(
                """
                SELECT catalog_id
                FROM regional_catalogs
                WHERE source_system = ? AND region = ? AND quarter = ?
                """,
                (source_system, region, quarter),
            )
        ),
    }
    for existing_catalog_id in catalog_ids:
        conn.execute(
            "DELETE FROM regional_catalogs WHERE catalog_id = ?",
            (existing_catalog_id,),
        )


def import_pdf(
    conn: sqlite3.Connection,
    *,
    pdf_path: Path,
    catalog_id: str,
    source_system: str,
    region: str,
    quarter: str,
) -> dict[str, object]:
    imported_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    sha256 = file_sha256(pdf_path)

    with pdfplumber.open(pdf_path) as pdf:
        metadata = pdf.metadata or {}
        page_count = len(pdf.pages)
        page_texts = [(idx, page.extract_text() or "") for idx, page in enumerate(pdf.pages, 1)]
        data_stand = infer_data_stand(text for _page, text in page_texts)
        title = (
            metadata.get("Title")
            or first_nonempty_line(page_texts[0][1] if page_texts else "")
            or "Hessenspezifische GOP"
        )

        conn.execute(
            """
            INSERT INTO regional_catalogs(
                catalog_id, source_system, region, quarter, title, source_file,
                source_url, data_stand, imported_at, page_count, sha256
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                catalog_id,
                source_system,
                region,
                quarter,
                title,
                str(pdf_path),
                None,
                data_stand,
                imported_at,
                page_count,
                sha256,
            ),
        )

        counts = {
            "catalog_id": catalog_id,
            "source_system": source_system,
            "region": region,
            "quarter": quarter,
            "pages": page_count,
            "raw_tables": 0,
            "raw_table_rows": 0,
            "regional_gops": 0,
            "rules": 0,
            "edges": 0,
            "payers": 0,
            "data_stand": data_stand,
        }

        for page_number, page in enumerate(pdf.pages, 1):
            page_text = page_texts[page_number - 1][1]
            conn.execute(
                """
                INSERT INTO regional_gop_pages(catalog_id, page, text)
                VALUES (?, ?, ?)
                """,
                (catalog_id, page_number, page_text),
            )
            counts["payers"] += import_payers_from_page(
                conn,
                catalog_id=catalog_id,
                page=page_number,
                text=page_text,
            )

            for table_index, table in enumerate(page.find_tables(), 1):
                rows = table.extract() or []
                if not rows:
                    continue
                counts["raw_tables"] += 1
                section = infer_section(page, table, page_text)
                header_json = json.dumps(rows[0], ensure_ascii=False)
                for row_index, row in enumerate(rows, 1):
                    conn.execute(
                        """
                        INSERT INTO regional_gop_tables(
                            catalog_id, page, table_index, row_index, header_json, cells_json
                        )
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            catalog_id,
                            page_number,
                            table_index,
                            row_index,
                            header_json,
                            json.dumps(row, ensure_ascii=False),
                        ),
                    )
                    counts["raw_table_rows"] += 1

                for entry in parse_table_entries(rows, section=section):
                    gop_id = insert_gop(
                        conn,
                        catalog_id=catalog_id,
                        source_system=source_system,
                        region=region,
                        quarter=quarter,
                        page=page_number,
                        table_index=table_index,
                        entry=entry,
                    )
                    counts["regional_gops"] += 1
                    rule_count, edge_count = insert_rules_and_edges(
                        conn,
                        catalog_id=catalog_id,
                        region=region,
                        quarter=quarter,
                        gop_id=gop_id,
                        gop_code=entry["variant"].code,
                        description=entry["description"],
                    )
                    counts["rules"] += rule_count
                    counts["edges"] += edge_count

    return counts


def parse_table_entries(rows: list[list[object]], *, section: str) -> Iterator[dict[str, object]]:
    if is_immunization_table(rows):
        yield from parse_immunization_table(rows, section=section)
        return

    header_index = find_standard_header(rows)
    start_index = header_index + 1 if header_index is not None else 0

    for row_index, row in enumerate(rows[start_index:], start_index + 1):
        cells = normalize_row(row, min_cols=3)
        if not cells:
            continue
        if is_standard_header_row(cells):
            continue
        gop_cell, description, valuation = cells[0], cells[1], cells[2]
        variants = expand_gop_cell(gop_cell)
        if not variants:
            continue
        for variant in variants:
            yield {
                "variant": variant,
                "section": section,
                "title": title_from_description(description),
                "description": normalize_cell(description),
                "valuation_text": one_line(valuation),
                "euro": parse_euro(valuation),
                "points": None,
                "unit": "EUR" if parse_euro(valuation) is not None else None,
                "role": None,
                "row_index": row_index,
                "raw_row_json": json.dumps(row, ensure_ascii=False),
            }


def parse_immunization_table(rows: list[list[object]], *, section: str) -> Iterator[dict[str, object]]:
    header = normalize_row(rows[0], min_cols=5)
    roles = [
        "Erste Dosen",
        "Letzte Dosis",
        "Auffrischimpfung",
    ]
    for row_index, row in enumerate(rows[1:], 2):
        cells = normalize_row(row, min_cols=5)
        if not cells:
            continue
        title = one_line(cells[0])
        valuation = cells[4]
        if not any(expand_gop_cell(cells[col]) for col in (1, 2, 3)):
            continue
        for col, role in zip((1, 2, 3), roles):
            variants = expand_gop_cell(cells[col])
            if not variants:
                continue
            header_role = one_line(header[col]) if col < len(header) else role
            for variant in variants:
                yield {
                    "variant": variant,
                    "section": section or "Impfungen",
                    "title": title_from_description(title),
                    "description": normalize_cell(title),
                    "valuation_text": one_line(valuation),
                    "euro": parse_euro(valuation),
                    "points": None,
                    "unit": "EUR" if parse_euro(valuation) is not None else None,
                    "role": header_role or role,
                    "row_index": row_index,
                    "raw_row_json": json.dumps(row, ensure_ascii=False),
                }


def insert_gop(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    source_system: str,
    region: str,
    quarter: str,
    page: int,
    table_index: int,
    entry: dict[str, object],
) -> int:
    variant = entry["variant"]
    assert isinstance(variant, GopVariant)
    cursor = conn.execute(
        """
        INSERT INTO regional_gops(
            catalog_id, source_system, region, quarter,
            gop_original, gop_code, gop_base, gop_suffix, markers, footnotes,
            section, title, description, valuation_text, euro, points, unit, role,
            page, table_index, row_index, raw_row_json
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            catalog_id,
            source_system,
            region,
            quarter,
            variant.original,
            variant.code,
            variant.base,
            variant.suffix or None,
            variant.markers or None,
            variant.footnotes or None,
            entry["section"],
            entry["title"],
            entry["description"],
            entry["valuation_text"],
            entry["euro"],
            entry["points"],
            entry["unit"],
            entry["role"],
            page,
            table_index,
            entry["row_index"],
            entry["raw_row_json"],
        ),
    )
    return int(cursor.lastrowid)


def insert_rules_and_edges(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    region: str,
    quarter: str,
    gop_id: int,
    gop_code: str,
    description: str,
) -> tuple[int, int]:
    rule_count = 0
    edge_count = 0
    for rule_text in extract_rule_fragments(description):
        rule_type = classify_rule(rule_text)
        conn.execute(
            """
            INSERT INTO regional_gop_rules(
                catalog_id, gop_id, quarter, region, gop_code,
                rule_type, rule_text, source_text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                catalog_id,
                gop_id,
                quarter,
                region,
                gop_code,
                rule_type,
                rule_text,
                description,
            ),
        )
        rule_count += 1

        relation_type = edge_relation_type(rule_type)
        for target_code in sorted(extract_gop_references(rule_text) - {gop_code}):
            conn.execute(
                """
                INSERT INTO regional_gop_edges(
                    catalog_id, source_gop_id, source_gop_code, target_gop_code,
                    relation_type, context, source_text
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    catalog_id,
                    gop_id,
                    gop_code,
                    target_code,
                    relation_type,
                    rule_type,
                    rule_text,
                ),
            )
            edge_count += 1
    return rule_count, edge_count


def import_payers_from_page(
    conn: sqlite3.Connection,
    *,
    catalog_id: str,
    page: int,
    text: str,
) -> int:
    in_payer_block = False
    count = 0
    context = page_heading(text) or "Vertragspartner"
    for raw_line in text.splitlines():
        line = one_line(raw_line)
        if line == "Kasse VKNR Kasse VKNR":
            in_payer_block = True
            continue
        if in_payer_block and line.startswith("Die jeweiligen Leistungsinhalte"):
            break
        if not in_payer_block:
            continue

        matches = list(re.finditer(r"\b\d{5}\b", line))
        start = 0
        for match in matches:
            payer_name = line[start : match.start()].strip(" -")
            vknr = match.group(0)
            start = match.end()
            if not payer_name:
                continue
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO regional_gop_payers(
                    catalog_id, page, context, payer_name, vknr
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    catalog_id,
                    page,
                    context,
                    payer_name,
                    vknr,
                ),
            )
            count += max(cursor.rowcount, 0)
    return count


def expand_gop_cell(value: object) -> list[GopVariant]:
    text = one_line(value)
    if not text or text.upper() == "GOP":
        return []

    matches = list(re.finditer(r"\d{5}", text))
    variants: list[GopVariant] = []
    for idx, match in enumerate(matches):
        start = match.start()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        segment = text[start:end].strip(" ,;/")
        variants.extend(parse_gop_segment(segment))
    return variants


def parse_gop_segment(segment: str) -> list[GopVariant]:
    match = re.search(r"(\d{5})", segment)
    if not match:
        return []

    base = match.group(1)
    tail = segment[match.end() :].strip()
    markers = "".join(sorted(set(re.findall(r"\*+", tail)), key=len))
    tail_without_markers = re.sub(r"\*+", " ", tail)
    suffix_tokens = re.findall(r"[A-Z](?:\d+)?", tail_without_markers)
    footnote_tokens = re.findall(r"(?<![A-Z])\b\d+\b", tail_without_markers)

    suffixes: list[str] = []
    footnotes = set(footnote_tokens)
    for token in suffix_tokens:
        split_token = split_suffix_footnote(base, token)
        suffixes.append(split_token[0])
        if split_token[1]:
            footnotes.add(split_token[1])

    if not suffixes:
        suffixes = [""]

    variants = []
    for suffix in dedupe_preserve_order(suffixes):
        variants.append(
            GopVariant(
                original=segment,
                code=f"{base}{suffix}",
                base=base,
                suffix=suffix,
                markers=markers,
                footnotes=",".join(sorted(footnotes, key=int)) if footnotes else "",
            )
        )
    return variants


def split_suffix_footnote(base: str, token: str) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Z]\d+", token):
        return token, ""
    if base.startswith(("88", "89")) and token[0] in {"R", "X"}:
        return token, ""
    return token[0], token[1:]


def extract_rule_fragments(description: str) -> list[str]:
    if not description:
        return []

    text = description.replace(BULLET, "\n- ")
    parts = re.split(r"\n\s*-\s*", text)
    if len(parts) <= 1:
        return []
    return [one_line(part) for part in parts[1:] if one_line(part)]


def classify_rule(rule_text: str) -> str:
    lower = rule_text.lower()
    if "nicht neben" in lower:
        return "EXCLUDES"
    if "nicht ohne" in lower or "nur mit" in lower:
        return "REQUIRES"
    if "höchstens" in lower or "hoechstens" in lower or "maximal" in lower:
        return "LIMIT"
    if "genehmigungspflichtig" in lower:
        return "REQUIRES_APPROVAL"
    if "ausschließlich" in lower or "ausschliesslich" in lower:
        return "ELIGIBILITY"
    return "BILLING_RULE"


def edge_relation_type(rule_type: str) -> str:
    if rule_type == "EXCLUDES":
        return "EXCLUDES"
    if rule_type == "REQUIRES":
        return "REQUIRES"
    return "REFERENCES"


def extract_gop_references(text: str) -> set[str]:
    refs = set()
    for match in re.finditer(r"\b(\d{5})([A-Z](?:\d+)?)?\b", text):
        refs.add(match.group(1) + (match.group(2) or ""))
    return refs


def infer_section(page, table, page_text: str) -> str:
    try:
        top = max(float(table.bbox[1]) - 2.0, 0.0)
        text_above = page.crop((0, 0, page.width, top)).extract_text() or ""
    except Exception:
        text_above = page_text

    candidates = []
    for raw_line in text_above.splitlines():
        line = one_line(raw_line)
        if not line:
            continue
        if line.startswith("Hessenspezifische Gebührenordnungspositionen"):
            continue
        if line.startswith("Stand:"):
            continue
        if is_standard_header_line(line):
            continue
        if line.startswith("-") or line.startswith(""):
            continue
        if len(line) > 120:
            continue
        candidates.append(line)
    if candidates:
        return candidates[-1]
    return page_heading(page_text)


def page_heading(page_text: str) -> str:
    lines = [one_line(line) for line in page_text.splitlines() if one_line(line)]
    for line in lines:
        if line.startswith("Hessenspezifische Gebührenordnungspositionen"):
            continue
        if line.startswith("Stand:"):
            continue
        return line
    return ""


def infer_data_stand(texts: Iterable[str]) -> str:
    counts: dict[str, int] = {}
    for text in texts:
        for match in re.finditer(r"Stand:\s*(\d{2}\.\d{2}\.\d{4})", text):
            counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: (item[1], item[0]))[0]


def find_standard_header(rows: list[list[object]]) -> Optional[int]:
    for idx, row in enumerate(rows):
        if is_standard_header_row(normalize_row(row, min_cols=3)):
            return idx
    return None


def is_standard_header_row(cells: list[str]) -> bool:
    return (
        len(cells) >= 3
        and one_line(cells[0]).upper() == "GOP"
        and "Bezeichnung" in one_line(cells[1])
        and "Bewertung" in one_line(cells[2])
    )


def is_standard_header_line(line: str) -> bool:
    return re.fullmatch(r"GOP\s+Bezeichnung\s+Bewertung", line) is not None


def is_immunization_table(rows: list[list[object]]) -> bool:
    if not rows:
        return False
    header = normalize_row(rows[0], min_cols=5)
    return bool(header and header[0] == "Impfungen" and len(header) >= 5)


def normalize_row(row: list[object], *, min_cols: int) -> list[str]:
    if row is None:
        return []
    cells = [normalize_cell(cell) for cell in row]
    while len(cells) < min_cols:
        cells.append("")
    return cells


def normalize_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", "\n")
    text = text.replace(BULLET, "\n- ")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r" *\n+ *", "\n", text)
    return text.strip()


def one_line(value: object) -> str:
    return re.sub(r"\s+", " ", normalize_cell(value)).strip()


def title_from_description(description: object) -> str:
    text = normalize_cell(description)
    if not text:
        return ""

    before_bullets = text.split("\n- ", 1)[0]
    lines = [one_line(line) for line in before_bullets.splitlines() if one_line(line)]
    if not lines:
        return one_line(text)[:240]

    title_parts: list[str] = []
    for line in lines:
        title_parts.append(line)
        if len(" ".join(title_parts)) >= 160:
            break
    return " ".join(title_parts)[:240]


def parse_euro(value: object) -> Optional[float]:
    text = one_line(value)
    match = re.search(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*" + re.escape(EURO), text)
    if not match:
        return None
    return float(match.group(1).replace(".", "").replace(",", "."))


def first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = one_line(line)
        if cleaned:
            return cleaned
    return ""


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


if __name__ == "__main__":
    sys.exit(main())
