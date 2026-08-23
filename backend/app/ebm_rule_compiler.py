from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .catalog import canonical_gop, normalize_gop


GOP_PATTERN = re.compile(r"(?<!\d)(\d{5}[A-Z0-9*]?)(?!\d)", re.IGNORECASE)
SCOPE_PATTERNS = (
    ("same_session", re.compile(r"\bin derselben sitzung\b", re.IGNORECASE)),
    ("treatment_day", re.compile(r"\bam behandlungstag\b", re.IGNORECASE)),
    ("treatment_case", re.compile(r"\bim behandlungsfall\b", re.IGNORECASE)),
    ("disease_case", re.compile(r"\bim krankheitsfall\b", re.IGNORECASE)),
    ("quarter", re.compile(r"\bim quartal\b", re.IGNORECASE)),
)
NUMBER_WORDS = {
    "einmal": 1,
    "zweimal": 2,
    "dreimal": 3,
    "viermal": 4,
    "fünfmal": 5,
    "sechsmal": 6,
}


@dataclass(frozen=True)
class CompiledRuleClause:
    clause_type: str
    scope: str | None
    parameters: dict[str, Any]
    source_text: str
    machine_executable: bool
    review_required: bool
    confidence: float


@dataclass(frozen=True)
class CompiledGopRule:
    rule_id: str
    definition_type: str
    source_type: str
    source_catalog_id: str | None
    quarter: str
    region: str
    catalog_key: str
    gop: str | None
    gop_base: str | None
    title: str
    source_text: str
    source_reference: dict[str, Any]
    scope: dict[str, Any]
    clauses: tuple[CompiledRuleClause, ...]
    coverage_status: str

    def definition_payload(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "type": self.definition_type,
            "catalog_key": self.catalog_key,
            "gop": self.gop,
            "gop_base": self.gop_base,
            "quarter": self.quarter,
            "region": self.region,
            "scope": self.scope,
            "clauses": [asdict(clause) for clause in self.clauses],
        }


@dataclass(frozen=True)
class CompiledCatalogRuleSet:
    rule_set_id: str
    version: str
    quarter: str
    region: str
    source_catalog_id: str | None
    source_data_stand: str | None
    source_hash: str
    compiled_at: str
    rules: tuple[CompiledGopRule, ...]
    summary: dict[str, Any] = field(default_factory=dict)


class RuleCompilationError(RuntimeError):
    pass


def compile_catalog_quarter(
    db_path: Path,
    quarter: str,
    region: str = "Hessen",
    *,
    include_regional: bool = True,
    gop_bases: Iterable[str] | None = None,
) -> CompiledCatalogRuleSet:
    if not re.fullmatch(r"\d{4}/Q[1-4]", quarter.strip(), re.IGNORECASE):
        raise RuleCompilationError("Das Quartal muss im Format JJJJ/Q1 bis JJJJ/Q4 angegeben werden.")
    quarter = quarter.strip().upper()
    region = region.strip() or "Hessen"
    if not db_path.exists() or not db_path.is_file():
        raise RuleCompilationError(f"Katalogdatenbank nicht gefunden: {db_path}")
    requested_bases = {
        normalize_gop(str(gop))[0]
        for gop in (gop_bases or ())
        if re.fullmatch(r"\d{4,5}[A-Z0-9*]*", str(gop).strip(), re.IGNORECASE)
    }
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row["name"] for row in conn.execute("select name from sqlite_master where type='table'")}
        if "details" not in tables:
            raise RuleCompilationError("Die Katalogdatenbank enthält keine EBM-Detailtabelle.")
        snapshot = _snapshot(conn, quarter, tables)
        sources = _ebm_sources(conn, quarter, requested_bases)
        if include_regional and "regional_gops" in tables:
            sources.extend(_regional_sources(conn, quarter, region, requested_bases, tables))
    finally:
        conn.close()

    if not sources:
        raise RuleCompilationError(f"Für {quarter} wurden keine Katalogregeln gefunden.")

    rules = tuple(compile_gop_rule(**source) for source in sources)
    digest = hashlib.sha256()
    for rule in sorted(rules, key=lambda item: (item.source_type, item.gop or item.catalog_key, item.rule_id)):
        digest.update(rule.source_type.encode("utf-8"))
        digest.update((rule.gop or rule.catalog_key).encode("utf-8"))
        digest.update(rule.title.encode("utf-8"))
        digest.update(rule.source_text.encode("utf-8"))
        digest.update(json.dumps(rule.source_reference, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    source_hash = digest.hexdigest()
    version = f"{quarter.lower().replace('/', '-')}-{source_hash[:12]}"
    summary = _summary(rules)
    summary.update(
        {
            "quarter": quarter,
            "region": region,
            "source_detail_count": len(sources),
            "all_source_texts_preserved": True,
        }
    )
    return CompiledCatalogRuleSet(
        rule_set_id="kbv-ebm-hessen-compiled",
        version=version,
        quarter=quarter,
        region=region,
        source_catalog_id=f"ebm_kbv_{quarter.lower().replace('/', '_')}",
        source_data_stand=snapshot.get("data_stand"),
        source_hash=source_hash,
        compiled_at=datetime.now(timezone.utc).isoformat(),
        rules=rules,
        summary=summary,
    )


def compile_gop_rule(
    *,
    definition_type: str,
    source_type: str,
    source_catalog_id: str | None,
    quarter: str,
    region: str,
    catalog_key: str,
    gop: str | None,
    title: str,
    source_text: str,
    source_reference: dict[str, Any],
    scope: dict[str, Any],
) -> CompiledGopRule:
    canonical = canonical_gop(gop) if gop else None
    gop_base = normalize_gop(canonical)[0] if canonical else None
    cleaned = _clean(source_text)
    clauses: list[CompiledRuleClause] = []
    clauses.extend(_exclusion_clauses(cleaned, gop_base or ""))
    clauses.extend(_frequency_clauses(cleaned))
    clauses.extend(_quantity_unit_clauses(cleaned))
    clauses.extend(_required_gop_clauses(cleaned, gop_base or ""))
    clauses.extend(_context_requirement_clauses(cleaned))
    clauses.extend(_age_clauses(cleaned))
    clauses.extend(_duration_clauses(cleaned))
    clauses.extend(_time_clauses(cleaned))
    clauses.extend(_authorization_clauses(cleaned))
    clauses.extend(_location_clauses(cleaned))
    clauses.extend(_reporting_clauses(cleaned))
    clauses.extend(_reference_clauses(cleaned))
    clauses.extend(_obligatory_content_clauses(cleaned))
    deduplicated = tuple(_deduplicate_clauses(clauses))
    coverage_status = "partial" if deduplicated else "text_only"
    source_token = "ebm" if source_type == "EBM_KBV" else "regional"
    reference_token = hashlib.sha256(
        json.dumps(source_reference, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:10]
    rule_subject = canonical or "context"
    return CompiledGopRule(
        rule_id=f"catalog.{source_token}.{quarter.lower().replace('/', '.')}.{rule_subject}.{reference_token}.v1",
        definition_type=definition_type,
        source_type=source_type,
        source_catalog_id=source_catalog_id,
        quarter=quarter,
        region=region,
        catalog_key=catalog_key,
        gop=canonical,
        gop_base=gop_base,
        title=title or canonical or catalog_key,
        source_text=cleaned,
        source_reference=source_reference,
        scope=scope,
        clauses=deduplicated,
        coverage_status=coverage_status,
    )


def _snapshot(conn: sqlite3.Connection, quarter: str, tables: set[str]) -> dict[str, Any]:
    if "snapshots" not in tables:
        return {}
    row = conn.execute(
        "select quarter, source_url, site_version, data_stand, retrieved_at from snapshots where quarter = ?",
        (quarter,),
    ).fetchone()
    return dict(row) if row else {}


def _ebm_sources(
    conn: sqlite3.Connection,
    quarter: str,
    requested_bases: set[str],
) -> list[dict[str, Any]]:
    detail_columns = {row["name"] for row in conn.execute("pragma table_info(details)")}
    text_expression = "d.text" if "text" in detail_columns else "d.title"
    rows = list(
        conn.execute(
            f"select d.row_key, d.gop, d.title, {text_expression} as text, "
            "n.parent_row_key, n.label from details d "
            "left join nodes n on n.quarter = d.quarter and n.row_key = d.row_key "
            "where d.quarter = ? order by d.row_key",
            (quarter,),
        )
    )
    nodes = {
        row["row_key"]: {"parent_row_key": row["parent_row_key"], "label": row["label"]}
        for row in conn.execute(
            "select row_key, parent_row_key, label from nodes where quarter = ?",
            (quarter,),
        )
    }
    direct_by_row = {
        str(row["row_key"]): normalize_gop(str(row["gop"]))[0]
        for row in rows
        if _is_billable_gop(str(row["gop"] or ""))
    }
    all_direct_gops = sorted(set(direct_by_row.values()))
    result: list[dict[str, Any]] = []
    for row in rows:
        row_key = str(row["row_key"])
        catalog_key = str(row["gop"] or row["title"] or row_key).strip()
        is_direct = _is_billable_gop(catalog_key)
        scope = (
            {"kind": "gop", "affected_gops": [normalize_gop(catalog_key)[0]]}
            if is_direct
            else _context_scope(row_key, catalog_key, direct_by_row, all_direct_gops)
        )
        if requested_bases and not _scope_intersects(scope, requested_bases):
            continue
        result.append(
            {
                "definition_type": "catalog_validation" if is_direct else "catalog_context",
                "source_type": "EBM_KBV",
                "source_catalog_id": f"ebm_kbv_{quarter.lower().replace('/', '_')}",
                "quarter": quarter,
                "region": "*",
                "catalog_key": catalog_key,
                "gop": catalog_key if is_direct else None,
                "title": str(row["title"] or catalog_key),
                "source_text": str(row["text"] or row["title"] or ""),
                "source_reference": {
                    "table": "details",
                    "row_key": row_key,
                    "parent_row_key": row["parent_row_key"],
                    "path": _node_path(row_key, nodes),
                },
                "scope": scope,
            }
        )
    return result


def _regional_sources(
    conn: sqlite3.Connection,
    quarter: str,
    region: str,
    requested_bases: set[str],
    tables: set[str],
) -> list[dict[str, Any]]:
    query = (
        "select id, catalog_id, source_system, gop_code, gop_base, title, description, page "
        "from regional_gops where quarter = ? and region = ?"
    )
    params: list[Any] = [quarter, region]
    if requested_bases:
        placeholders = ",".join("?" for _ in requested_bases)
        query += f" and gop_base in ({placeholders})"
        params.extend(sorted(requested_bases))
    query += " order by gop_code, id"
    result: list[dict[str, Any]] = []
    variant_counts: Counter[tuple[Any, ...]] = Counter()
    for row in conn.execute(query, params):
        texts = [str(row["description"] or "")]
        if "regional_gop_rules" in tables:
            texts.extend(
                str(rule["rule_text"])
                for rule in conn.execute(
                    "select rule_text from regional_gop_rules where catalog_id = ? "
                    "and (gop_id = ? or gop_code = ? or gop_code = ?) order by id",
                    (row["catalog_id"], row["id"], row["gop_code"], row["gop_base"]),
                )
                if rule["rule_text"]
            )
        source_text = " ".join(text for text in texts if text.strip())
        variant_signature = (
            row["catalog_id"],
            row["gop_code"],
            row["title"],
            row["page"],
            _clean(source_text),
        )
        variant_index = variant_counts[variant_signature]
        variant_counts[variant_signature] += 1
        result.append(
            {
                "definition_type": "catalog_validation",
                "source_type": str(row["source_system"] or "KV_HESSEN_GOP"),
                "source_catalog_id": row["catalog_id"],
                "quarter": quarter,
                "region": region,
                "catalog_key": str(row["gop_code"]),
                "gop": str(row["gop_code"]),
                "title": str(row["title"] or row["gop_code"]),
                "source_text": source_text,
                "source_reference": {
                    "table": "regional_gops",
                    "catalog_id": row["catalog_id"],
                    "gop_code": row["gop_code"],
                    "title": row["title"],
                    "page": row["page"],
                    "variant_index": variant_index,
                },
                "scope": {"kind": "gop", "affected_gops": [str(row["gop_base"])]},
            }
        )
    return result


def _exclusion_clauses(text: str, own_gop: str) -> list[CompiledRuleClause]:
    marker = re.search(r"\bAbrechnungsausschlüsse\b", text, re.IGNORECASE)
    if not marker:
        return []
    section = text[marker.start() :]
    scope_hits: list[tuple[int, int, str]] = []
    for scope, pattern in SCOPE_PATTERNS:
        scope_hits.extend((match.start(), match.end(), scope) for match in pattern.finditer(section))
    scope_hits.sort()
    clauses: list[CompiledRuleClause] = []
    if not scope_hits:
        gops = _other_gops(section, own_gop)
        if gops:
            clauses.append(_clause("exclusion", None, {"gops": gops}, section, True, False, 0.88))
        return clauses
    prefix = section[: scope_hits[0][0]]
    prefix_gops = _other_gops(prefix, own_gop)
    if prefix_gops:
        clauses.append(
            _clause(
                "exclusion",
                scope_hits[0][2],
                {"gops": prefix_gops},
                section[: scope_hits[0][1]],
                True,
                False,
                0.9,
            )
        )
    for index, (start, end, scope) in enumerate(scope_hits):
        segment_end = scope_hits[index + 1][0] if index + 1 < len(scope_hits) else len(section)
        excerpt = section[start:segment_end]
        gops = _other_gops(excerpt, own_gop)
        if gops:
            clauses.append(_clause("exclusion", scope, {"gops": gops}, excerpt, True, False, 0.96))
    return clauses


def _frequency_clauses(text: str) -> list[CompiledRuleClause]:
    number = r"(?:einmal|zweimal|dreimal|viermal|fünfmal|sechsmal|\d+\s*mal)"
    scope = (
        r"(?:Sitzung|Behandlungstag|Kalendertag|Behandlungsfall|Krankheitsfall|Arztfall|"
        r"Arztgruppenfall|Versichertenfall|Quartal|Kalendervierteljahr|Kalenderwoche|Kalenderjahr)"
    )
    pattern = re.compile(
        rf"\b(?:(höchstens|maximal)\s+)?({number})\s+"
        rf"(?:je|im|pro|am|in\s+der|in\s+derselben)\s+({scope})\b",
        re.IGNORECASE,
    )
    clauses: list[CompiledRuleClause] = []
    matched_spans: list[tuple[int, int]] = []
    for match in pattern.finditer(text):
        maximum = _number_value(match.group(2))
        if maximum is None:
            continue
        matched_spans.append(match.span())
        clauses.append(
            _clause(
                "frequency_limit",
                _scope_name(match.group(3)),
                {"maximum": maximum},
                _excerpt(text, match.start(), match.end()),
                True,
                False,
                0.94 if match.group(1) else 0.9,
            )
        )
    per_unit_pattern = re.compile(r"\bje\s+(Sitzung|Behandlungstag|Kalendertag)\b", re.IGNORECASE)
    for match in per_unit_pattern.finditer(text):
        if any(start <= match.start() and match.end() <= end for start, end in matched_spans):
            continue
        clauses.append(
            _clause(
                "frequency_limit",
                _scope_name(match.group(1)),
                {"maximum": 1},
                _excerpt(text, match.start(), match.end()),
                True,
                False,
                0.94,
            )
        )
    return clauses


def _quantity_unit_clauses(text: str) -> list[CompiledRuleClause]:
    pattern = re.compile(
        r"\bje\s+(Untersuchung|Seite|Organ|Extremität)\b",
        re.IGNORECASE,
    )
    return [
        _clause(
            "quantity_unit",
            None,
            {"unit": _clean(match.group(1)).casefold()},
            _excerpt(text, match.start(), match.end()),
            False,
            True,
            0.82,
        )
        for match in pattern.finditer(text)
    ]


def _required_gop_clauses(text: str, own_gop: str) -> list[CompiledRuleClause]:
    pre_exclusions = re.split(r"\bAbrechnungsausschlüsse\b", text, maxsplit=1, flags=re.IGNORECASE)[0]
    patterns = (
        re.compile(r"Zuschlag\s+zu\s+(?:der\s+)?Gebührenordnungsposition(?:en)?\s+([^.;]{0,180})", re.IGNORECASE),
        re.compile(r"(?:setzt|setzen)\s+([^.;]{0,220})\s+voraus", re.IGNORECASE),
        re.compile(r"(?:nur|ausschließlich)\s+(?:neben|in Verbindung mit)\s+([^.;]{0,180})", re.IGNORECASE),
    )
    clauses: list[CompiledRuleClause] = []
    for pattern in patterns:
        for match in pattern.finditer(pre_exclusions):
            gops = _other_gops(match.group(0), own_gop)
            if gops:
                clauses.append(
                    _clause(
                        "requires_gop",
                        "treatment_case",
                        {"gops": gops, "mode": "any" if len(gops) > 1 else "all"},
                        _excerpt(pre_exclusions, match.start(), match.end()),
                        True,
                        False,
                        0.9,
                    )
                )
    return clauses


def _context_requirement_clauses(text: str) -> list[CompiledRuleClause]:
    clauses: list[CompiledRuleClause] = []
    if re.search(r"\bICD-10(?:-GM)?\b", text, re.IGNORECASE):
        clauses.append(
            _clause(
                "requires_icd",
                "treatment_case",
                {"diagnosis_certainty_required": bool(re.search(r"Diagnosensicherheit", text, re.IGNORECASE))},
                _matching_excerpt(text, r"ICD-10(?:-GM)?[^.]{0,220}"),
                True,
                False,
                0.9,
            )
        )
    if re.search(r"persönliche[nrms]?\s+Arzt-Patienten-Kontakt", text, re.IGNORECASE):
        clauses.append(
            _clause(
                "requires_personal_contact",
                "service",
                {"contact_type": "personal"},
                _matching_excerpt(text, r"[^.]{0,100}persönliche[nrms]?\s+Arzt-Patienten-Kontakt[^.]{0,120}"),
                True,
                False,
                0.96,
            )
        )
    return clauses


def _age_clauses(text: str) -> list[CompiledRuleClause]:
    clauses: list[CompiledRuleClause] = []
    patterns = (
        (re.compile(r"bis\s+zum\s+vollendeten\s+(\d{1,3})\.?\s+Lebensjahr", re.IGNORECASE), "max", -1),
        (re.compile(r"ab\s+dem\s+vollendeten\s+(\d{1,3})\.?\s+Lebensjahr", re.IGNORECASE), "min", 0),
        (re.compile(r"ab\s+Beginn\s+des\s+(\d{1,3})\.?\s+Lebensjahres", re.IGNORECASE), "min", -1),
    )
    for pattern, boundary, offset in patterns:
        for match in pattern.finditer(text):
            age = max(0, int(match.group(1)) + offset)
            clauses.append(
                _clause(
                    "age_limit",
                    "patient",
                    {f"{boundary}_age": age, "wording": _clean(match.group(0))},
                    _excerpt(text, match.start(), match.end()),
                    True,
                    False,
                    0.9,
                )
            )
    return clauses


def _duration_clauses(text: str) -> list[CompiledRuleClause]:
    minimum_pattern = re.compile(r"\bmindestens\s+(\d{1,3})\s+Minuten\b", re.IGNORECASE)
    clauses = [
        _clause(
            "minimum_duration",
            "service",
            {"minutes": int(match.group(1))},
            _excerpt(text, match.start(), match.end()),
            False,
            True,
            0.92,
        )
        for match in minimum_pattern.finditer(text)
    ]
    increment_pattern = re.compile(
        r"\bje\s+(?:(weitere)\s+)?vollendete\s+(\d{1,3})\s+Minuten\b",
        re.IGNORECASE,
    )
    clauses.extend(
        _clause(
            "duration_increment",
            "service",
            {"minutes": int(match.group(2)), "additional_increment": bool(match.group(1))},
            _excerpt(text, match.start(), match.end()),
            False,
            True,
            0.94,
        )
        for match in increment_pattern.finditer(text)
    )
    return clauses


def _time_clauses(text: str) -> list[CompiledRuleClause]:
    pattern = re.compile(
        r"\bzwischen\s+(\d{1,2})(?::(\d{2}))?\s*(?:Uhr)?\s+und\s+(\d{1,2})(?::(\d{2}))?\s*Uhr\b",
        re.IGNORECASE,
    )
    return [
        _clause(
            "time_window",
            "service",
            {
                "start": f"{int(match.group(1)):02d}:{int(match.group(2) or 0):02d}",
                "end": f"{int(match.group(3)):02d}:{int(match.group(4) or 0):02d}",
            },
            _excerpt(text, match.start(), match.end()),
            True,
            False,
            0.9,
        )
        for match in pattern.finditer(text)
    ]


def _authorization_clauses(text: str) -> list[CompiledRuleClause]:
    pattern = re.compile(r"[^.]{0,120}\b(Genehmigung|Qualifikation|Qualifikationsvoraussetzung)\b[^.]{0,180}", re.IGNORECASE)
    return [
        _clause(
            "authorization",
            "provider",
            {"requirement": _clean(match.group(0))},
            _clean(match.group(0)),
            False,
            True,
            0.84,
        )
        for match in pattern.finditer(text)
    ]


def _location_clauses(text: str) -> list[CompiledRuleClause]:
    pattern = re.compile(
        r"[^.]{0,130}\b(Arztpraxis|Laborgemeinschaft|Krankenhaus|Belegarzt|ambulant|stationär)\b[^.]{0,180}",
        re.IGNORECASE,
    )
    return [
        _clause(
            "service_location",
            "service",
            {"requirement": _clean(match.group(0))},
            _clean(match.group(0)),
            False,
            True,
            0.78,
        )
        for match in pattern.finditer(text)
    ]


def _reporting_clauses(text: str) -> list[CompiledRuleClause]:
    match = re.search(r"\b(Nicht\s+berichtspflichtig|Berichtspflichtig)\b", text, re.IGNORECASE)
    if not match:
        return []
    required = not match.group(1).casefold().startswith("nicht")
    return [_clause("reporting", None, {"required": required}, match.group(0), True, False, 0.99)]


def _reference_clauses(text: str) -> list[CompiledRuleClause]:
    pattern = re.compile(
        r"[^.]{0,100}\b(?:Bestimmung(?:en)?\s+zum\s+Abschnitt|Präambel|Anhang)\b[^.]{0,160}",
        re.IGNORECASE,
    )
    return [
        _clause(
            "catalog_reference",
            None,
            {"reference": _clean(match.group(0))},
            _clean(match.group(0)),
            False,
            True,
            0.8,
        )
        for match in pattern.finditer(text)
    ]


# Der Katalog nennt bei knapp der Haelfte aller GOPs ausdruecklich, welche
# Leistung erbracht sein muss. Der Abschnitt endet an der naechsten Ueberschrift.
OBLIGATORY_CONTENT_PATTERN = re.compile(
    r"Obligater\s+Leistungsinhalt(?P<content>.*?)"
    r"(?=Fakultativer\s+Leistungsinhalt|Abrechnungsbestimmung|Anmerkung|Berichtspflicht"
    r"|Abrechnungsausschl|Kalkulationszeit|Aufwand\s+in\s+Min|Beschreibung|$)",
    re.IGNORECASE | re.DOTALL,
)
# Der Pflichtinhalt ist eine Aufzaehlung. Kommas trennen die Elemente, aber nicht
# innerhalb von Klammern und nicht in Verweisen wie "Anlage I a und Anlage I b".
CONTENT_ELEMENT_SPLIT = re.compile(r",(?![^(]*\))")
MIN_CONTENT_ELEMENT_LENGTH = 4


def _obligatory_content_clauses(text: str) -> list[CompiledRuleClause]:
    """Pflichtinhalt einer GOP als pruefbare Klausel.

    Bisher pruefte das Tor nur Nebenbedingungen - Ausschluesse, Haeufigkeiten,
    Alter, Uhrzeit. Ob die beschriebene Leistung ueberhaupt erbracht wurde, blieb
    ungeprueft. Diese Klausel traegt die Elemente, die der Katalog verlangt; die
    Zuordnung zu Evidenz erfolgt semantisch, die Vollstaendigkeit prueft der Server.
    """
    match = OBLIGATORY_CONTENT_PATTERN.search(text)
    if not match:
        return []
    content = _clean(match.group("content"))
    elements = [
        element.strip(" ,;")
        for element in CONTENT_ELEMENT_SPLIT.split(content)
        if len(element.strip(" ,;")) >= MIN_CONTENT_ELEMENT_LENGTH
    ]
    if not elements:
        return []
    return [
        _clause(
            "required_service_content",
            "service",
            {"elements": elements},
            content,
            # Die Pruefung braucht eine semantische Zuordnung, ist also nicht
            # allein maschinell entscheidbar.
            False,
            True,
            0.9,
        )
    ]


def _summary(rules: tuple[CompiledGopRule, ...]) -> dict[str, Any]:
    clauses = [clause for rule in rules for clause in rule.clauses]
    definitions_with_machine_clauses = sum(
        1 for rule in rules if any(clause.machine_executable for clause in rule.clauses)
    )
    return {
        "definition_count": len(rules),
        "unique_gop_count": len({rule.gop_base for rule in rules if rule.gop_base}),
        "definition_types": dict(Counter(rule.definition_type for rule in rules)),
        "clause_count": len(clauses),
        "machine_clause_count": sum(1 for clause in clauses if clause.machine_executable),
        "review_clause_count": sum(1 for clause in clauses if clause.review_required),
        "definitions_with_machine_clauses": definitions_with_machine_clauses,
        "machine_definition_coverage": round(definitions_with_machine_clauses / len(rules), 4) if rules else 0,
        "coverage": dict(Counter(rule.coverage_status for rule in rules)),
        "clause_types": dict(Counter(clause.clause_type for clause in clauses)),
        "source_types": dict(Counter(rule.source_type for rule in rules)),
    }


def _clause(
    clause_type: str,
    scope: str | None,
    parameters: dict[str, Any],
    source_text: str,
    machine_executable: bool,
    review_required: bool,
    confidence: float,
) -> CompiledRuleClause:
    return CompiledRuleClause(
        clause_type=clause_type,
        scope=scope,
        parameters=parameters,
        source_text=_clean(source_text)[:2000],
        machine_executable=machine_executable,
        review_required=review_required,
        confidence=round(confidence, 3),
    )


def _deduplicate_clauses(clauses: list[CompiledRuleClause]) -> list[CompiledRuleClause]:
    result: list[CompiledRuleClause] = []
    seen: set[str] = set()
    for clause in clauses:
        signature = repr((clause.clause_type, clause.scope, sorted(clause.parameters.items()), clause.source_text))
        if signature not in seen:
            result.append(clause)
            seen.add(signature)
    return result


def _other_gops(text: str, own_gop: str) -> list[str]:
    return list(
        dict.fromkeys(
            canonical_gop(match.group(1))
            for match in GOP_PATTERN.finditer(text)
            if normalize_gop(match.group(1))[0] != own_gop
        )
    )


def _number_value(value: str) -> int | None:
    cleaned = re.sub(r"\s+", "", value.casefold())
    if cleaned in NUMBER_WORDS:
        return NUMBER_WORDS[cleaned]
    match = re.fullmatch(r"(\d+)mal", cleaned)
    return int(match.group(1)) if match else None


def _scope_name(value: str) -> str:
    folded = value.casefold()
    if "sitzung" in folded:
        return "same_session"
    if "behandlungstag" in folded:
        return "treatment_day"
    if "kalendertag" in folded:
        return "calendar_day"
    if "behandlungsfall" in folded:
        return "treatment_case"
    if "krankheitsfall" in folded:
        return "disease_case"
    if "arztgruppenfall" in folded:
        return "physician_group_case"
    if "arztfall" in folded:
        return "physician_case"
    if "versichertenfall" in folded:
        return "insured_case"
    if "kalenderwoche" in folded:
        return "calendar_week"
    if "kalenderjahr" in folded:
        return "calendar_year"
    return "quarter"


def _matching_excerpt(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.IGNORECASE)
    return _clean(match.group(0)) if match else ""


def _excerpt(text: str, start: int, end: int, radius: int = 120) -> str:
    return _clean(text[max(0, start - radius) : min(len(text), end + radius)])


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _is_billable_gop(value: str) -> bool:
    return bool(re.fullmatch(r"\d{5}[A-Z0-9*]*", value.strip(), re.IGNORECASE))


def _context_scope(
    row_key: str,
    catalog_key: str,
    direct_by_row: dict[str, str],
    all_direct_gops: list[str],
) -> dict[str, Any]:
    range_match = re.fullmatch(r"\s*(\d{5})\s*[-–]\s*(\d{5})\s*", catalog_key)
    if range_match:
        start, end = range_match.groups()
        return {"kind": "gop_range", "start": start, "end": end}
    if row_key.split("_", 1)[0] == "0":
        return {"kind": "global", "affected_gops": all_direct_gops}
    prefix = f"{row_key}_"
    affected = sorted({gop for key, gop in direct_by_row.items() if key.startswith(prefix)})
    if affected:
        return {"kind": "subtree", "affected_gops": affected}
    return {"kind": "catalog_context", "affected_gops": []}


def _scope_intersects(scope: dict[str, Any], requested_bases: set[str]) -> bool:
    kind = scope.get("kind")
    if kind == "global":
        return True
    if kind == "gop_range":
        start = str(scope.get("start") or "")
        end = str(scope.get("end") or "")
        return any(start <= gop <= end for gop in requested_bases)
    affected = {str(gop) for gop in scope.get("affected_gops", [])}
    return bool(affected.intersection(requested_bases))


def _node_path(row_key: str, nodes: dict[str, dict[str, Any]]) -> list[str]:
    path: list[str] = []
    current: str | None = row_key
    visited: set[str] = set()
    while current and current not in visited:
        visited.add(current)
        node = nodes.get(current)
        if not node:
            break
        label = _clean(str(node.get("label") or ""))
        if label:
            path.append(label)
        current = node.get("parent_row_key")
    return list(reversed(path))
