import sqlite3
from pathlib import Path

import pytest

from app.billing_rule_store import build_clause_rows, build_definition_rows
from app.ebm_rule_compiler import RuleCompilationError, compile_catalog_quarter, compile_gop_rule


def build_compiler_catalog(path: Path) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "create table snapshots (quarter text primary key, source_url text, site_version text, "
            "data_stand text, retrieved_at text, node_count integer, detail_count integer)"
        )
        conn.execute(
            "create table nodes (quarter text, row_key text, parent_row_key text, sort_index integer, "
            "level integer, label text, is_parent integer, is_leaf integer)"
        )
        conn.execute(
            "create table details (quarter text, row_key text, gop text, title text, text text)"
        )
        conn.execute(
            "insert into snapshots values ('2026/Q1', 'https://ebm.kbv.de/', '1.0', "
            "'01.01.2026', '2026-01-01T00:00:00+00:00', 8, 6)"
        )
        nodes = [
            ("0", None, "I Allgemeine Bestimmungen", 1, 0),
            ("0_0", "0", "1 Berechnungsfähige Leistungen", 0, 1),
            ("1", None, "II Arztgruppenübergreifende GOP", 1, 0),
            ("1_0", "1", "1.2 Notfallleistungen", 1, 0),
            ("1_0_0", "1_0", "01226 Zuschlag", 0, 1),
            ("1_0_0_0", "1_0", "01226 Altersvariante", 0, 1),
            ("1_0_1", "1_0", "32025 - 32026 Laborbereich", 1, 0),
            ("1_0_1_0", "1_0_1", "32025 Glucose", 0, 1),
        ]
        for index, (row_key, parent, label, is_parent, is_leaf) in enumerate(nodes):
            conn.execute(
                "insert into nodes values ('2026/Q1', ?, ?, ?, ?, ?, ?, ?)",
                (row_key, parent, index, row_key.count("_"), label, is_parent, is_leaf),
            )
        details = [
            (
                "0_0",
                "1",
                "Berechnungsfähige Leistungen",
                "Der Katalog der berechnungsfähigen Gebührenordnungspositionen ist abschließend.",
            ),
            (
                "1_0",
                "1.2",
                "Notfallleistungen",
                "Die Leistungen sind höchstens einmal im Behandlungsfall berechnungsfähig.",
            ),
            (
                "1_0_0",
                "01226",
                "Zuschlag Notfallpauschale",
                "Zuschlag zu der Gebührenordnungsposition 01212. ICD-10-GM und persönlicher "
                "Arzt-Patienten-Kontakt. Abrechnungsausschlüsse 01224 im Behandlungsfall.",
            ),
            (
                "1_0_0_0",
                "01226",
                "Zuschlag bis zum vollendeten 12. Lebensjahr",
                "Bis zum vollendeten 12. Lebensjahr höchstens einmal im Behandlungsfall.",
            ),
            (
                "1_0_1",
                "32025 - 32026",
                "Laborbereich",
                "Abrechnungsausschlüsse 32066 im Behandlungsfall.",
            ),
            (
                "1_0_1_0",
                "32025",
                "Glucose",
                "32025 Glucose, je Untersuchung. Nicht berichtspflichtig.",
            ),
        ]
        conn.executemany(
            "insert into details values ('2026/Q1', ?, ?, ?, ?)",
            details,
        )


def test_compiler_preserves_all_ebm_records_and_context(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    build_compiler_catalog(path)

    compiled = compile_catalog_quarter(path, "2026/Q1")

    assert compiled.summary["definition_count"] == 6
    assert compiled.summary["definition_types"] == {
        "catalog_context": 3,
        "catalog_validation": 3,
    }
    assert compiled.summary["all_source_texts_preserved"] is True
    assert sum(rule.source_type == "EBM_KBV" for rule in compiled.rules) == 6
    assert all(rule.source_text for rule in compiled.rules)


def test_compiler_keeps_gop_variants_and_supabase_keys_unique(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    build_compiler_catalog(path)

    compiled = compile_catalog_quarter(path, "2026/Q1")
    variants = [rule for rule in compiled.rules if rule.gop_base == "01226"]
    definition_rows = build_definition_rows(compiled)
    clause_rows = build_clause_rows(compiled)

    assert len(variants) == 2
    assert len({rule.rule_id for rule in variants}) == 2
    assert len({row["definition_key"] for row in definition_rows}) == len(definition_rows)
    assert len({row["clause_key"] for row in clause_rows}) == len(clause_rows)
    assert any(row["scope"]["kind"] == "gop_range" for row in definition_rows)


def test_compiler_extracts_generic_requirements_from_gop_text(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    build_compiler_catalog(path)

    compiled = compile_catalog_quarter(path, "2026/Q1")
    rule = next(
        rule
        for rule in compiled.rules
        if rule.gop_base == "01226" and rule.title == "Zuschlag Notfallpauschale"
    )
    clauses = {clause.clause_type: clause for clause in rule.clauses}

    assert clauses["requires_gop"].parameters["gops"] == ["01212"]
    assert clauses["requires_icd"].machine_executable is True
    assert clauses["requires_personal_contact"].machine_executable is True
    assert clauses["exclusion"].parameters["gops"] == ["01224"]


@pytest.mark.parametrize(
    ("legend", "scope"),
    [
        ("Pauschale, einmal am Behandlungstag", "treatment_day"),
        ("Pauschale, je Sitzung", "same_session"),
        ("Pauschale, einmal im Behandlungsfall", "treatment_case"),
        ("Pauschale, einmal im Krankheitsfall", "disease_case"),
    ],
)
def test_compiler_reads_flat_rate_frequency_from_legend(legend: str, scope: str) -> None:
    rule = compile_gop_rule(
        definition_type="catalog_validation",
        source_type="EBM_KBV",
        source_catalog_id="test",
        quarter="2026/Q2",
        region="*",
        catalog_key="99999",
        gop="99999",
        title="Testpauschale",
        source_text=legend,
        source_reference={"test": True},
        scope={"kind": "gop", "affected_gops": ["99999"]},
    )

    frequency = [clause for clause in rule.clauses if clause.clause_type == "frequency_limit"]

    assert len(frequency) == 1
    assert frequency[0].scope == scope
    assert frequency[0].parameters == {"maximum": 1}
    assert not any(clause.clause_type.startswith("duration") for clause in rule.clauses)


def test_compiler_creates_time_rules_only_for_explicit_duration_wording() -> None:
    rule = compile_gop_rule(
        definition_type="catalog_validation",
        source_type="EBM_KBV",
        source_catalog_id="test",
        quarter="2026/Q2",
        region="*",
        catalog_key="99999",
        gop="99999",
        title="Zeitgebundene Leistung",
        source_text=(
            "Persönlicher Arzt-Patienten-Kontakt, mindestens 10 Minuten. "
            "Zuschlag je weitere vollendete 5 Minuten."
        ),
        source_reference={"test": True},
        scope={"kind": "gop", "affected_gops": ["99999"]},
    )
    clauses = {clause.clause_type: clause for clause in rule.clauses}

    assert clauses["minimum_duration"].parameters == {"minutes": 10}
    assert clauses["duration_increment"].parameters == {
        "minutes": 5,
        "additional_increment": True,
    }
    assert not any(clause.clause_type == "frequency_limit" for clause in rule.clauses)


def test_selected_compilation_includes_matching_hierarchy_but_not_other_ranges(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    build_compiler_catalog(path)

    compiled = compile_catalog_quarter(path, "2026/Q1", gop_bases=("01226",))

    assert any(rule.gop_base == "01226" for rule in compiled.rules)
    assert any(rule.catalog_key == "1.2" for rule in compiled.rules)
    assert any(rule.scope["kind"] == "global" for rule in compiled.rules)
    assert not any(rule.catalog_key == "32025 - 32026" for rule in compiled.rules)
    assert not any(rule.gop_base == "32025" for rule in compiled.rules)


def test_compiler_rejects_invalid_quarter(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    build_compiler_catalog(path)

    with pytest.raises(RuleCompilationError, match="Format"):
        compile_catalog_quarter(path, "Q1-2026")


def test_identical_regional_rows_receive_unique_stable_variant_keys(tmp_path: Path) -> None:
    path = tmp_path / "catalog.sqlite"
    build_compiler_catalog(path)
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "create table regional_gops (id integer primary key autoincrement, catalog_id text, "
            "source_system text, region text, quarter text, gop_code text, gop_base text, "
            "title text, description text, page integer)"
        )
        for _ in range(2):
            conn.execute(
                "insert into regional_gops(catalog_id, source_system, region, quarter, gop_code, "
                "gop_base, title, description, page) values "
                "('hessen_2026_q1', 'KV_HESSEN_GOP', 'Hessen', '2026/Q1', '01226H', "
                "'01226', 'Regionaler Zuschlag', 'Regionale Regel', 7)"
            )

    compiled = compile_catalog_quarter(path, "2026/Q1")
    regional = [rule for rule in compiled.rules if rule.source_type == "KV_HESSEN_GOP"]

    assert len(regional) == 2
    assert len({rule.rule_id for rule in regional}) == 2
    assert [rule.source_reference["variant_index"] for rule in regional] == [0, 1]
