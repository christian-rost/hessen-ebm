import sqlite3
from pathlib import Path

from app.admin_catalog import install_catalog_database, validate_catalog_database
from app.catalog import CatalogRepository


def build_catalog(path: Path):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "create table snapshots (quarter text primary key, source_url text not null, "
            "site_version text, data_stand text, retrieved_at text not null, "
            "node_count integer not null default 0, detail_count integer not null default 0)"
        )
        conn.execute("create table nodes (quarter text not null, row_key text not null)")
        conn.execute("create table details (quarter text not null, row_key text not null, gop text, title text, points text, euro text)")
        conn.execute(
            "insert into snapshots values ('2025/Q4', 'https://ebm.kbv.de/', '1.4.0', "
            "'02.04.2026', '2026-06-16T00:00:00+00:00', 1, 1)"
        )
        conn.execute("insert into nodes values ('2025/Q4', 'n1')")
        conn.execute("insert into details values ('2025/Q4', 'n1', '01210', 'Notfallpauschale I', '120', '14.87')")


def add_ebm_rule_text(path: Path):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("alter table details add column text text")
        conn.execute(
            "update details set text = 'Die Uhrzeit der Inanspruchnahme ist anzugeben.' "
            "where quarter = '2025/Q4' and gop = '01210'"
        )


def add_regional_catalog(path: Path):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(
            "create table regional_catalogs (catalog_id text primary key, source_system text not null, "
            "region text not null, quarter text not null, title text, source_file text, source_url text, "
            "data_stand text, imported_at text not null, page_count integer not null default 0, "
            "sha256 text, unique (source_system, region, quarter))"
        )
        conn.execute(
            "create table regional_gops (id integer primary key autoincrement, catalog_id text not null, "
            "source_system text not null, region text not null, quarter text not null, gop_original text not null, "
            "gop_code text not null, gop_base text not null, title text, description text, euro real, points text, page integer)"
        )
        conn.execute(
            "create table regional_gop_rules (id integer primary key autoincrement, catalog_id text not null, "
            "gop_id integer, quarter text not null, region text not null, gop_code text, rule_type text not null, "
            "rule_text text not null, source_text text)"
        )
        conn.execute(
            "insert into regional_catalogs values ("
            "'kv_hessen_gop_2026_q2', 'KV_HESSEN_GOP', 'Hessen', '2026/Q2', "
            "'Hessen-GOP Q2', null, null, '01.04.2026', '2026-06-16T00:00:00+00:00', 10, 'abc')"
        )
        conn.execute(
            "insert into regional_gops(catalog_id, source_system, region, quarter, gop_original, gop_code, gop_base, "
            "title, description, euro, points, page) values ("
            "'kv_hessen_gop_2026_q2', 'KV_HESSEN_GOP', 'Hessen', '2026/Q2', '01210H', "
            "'01210H', '01210', 'Hessen-Zuschlag Notfall', 'Regionaler Zuschlag', 3.21, '26', 7)"
        )
        conn.execute(
            "insert into regional_gop_rules(catalog_id, quarter, region, gop_code, rule_type, rule_text, source_text) "
            "values ('kv_hessen_gop_2026_q2', '2026/Q2', 'Hessen', '01210H', 'payer', "
            "'Nur im regionalen Hessen-Kontext abrechnungsfähig.', 'Nur im regionalen Hessen-Kontext abrechnungsfähig.')"
        )


def test_validate_catalog_database(tmp_path):
    source = tmp_path / "catalog.sqlite"
    build_catalog(source)

    result = validate_catalog_database(source)

    assert result["valid"] is True
    assert result["counts"]["snapshots"] == 1
    assert result["snapshots"][0]["quarter"] == "2025/Q4"


def test_install_catalog_database_creates_backup(tmp_path):
    current = tmp_path / "active.sqlite"
    replacement = tmp_path / "replacement.sqlite"
    backup_dir = tmp_path / "backups"
    build_catalog(current)
    build_catalog(replacement)

    result = install_catalog_database(replacement, current, backup_dir)

    assert result["installed"] is True
    assert result["backup_path"]
    assert Path(result["backup_path"]).exists()
    assert validate_catalog_database(current)["counts"]["details"] == 1


def test_catalog_lookup_works_without_regional_tables(tmp_path):
    source = tmp_path / "ebm_only.sqlite"
    build_catalog(source)

    repo = CatalogRepository(source)

    entry = repo.lookup("01210", "2025/Q4")

    assert entry.title == "Notfallpauschale I"
    assert entry.catalog_id == "ebm_kbv_2025_q4"
    assert entry.catalog_label == "KBV EBM 2025/Q4"
    assert entry.data_stand == "02.04.2026"
    assert repo.lookup_hessen("01210", "2025/Q4") is None


def test_catalog_lookup_exposes_ebm_rule_text(tmp_path):
    source = tmp_path / "ebm_with_rule_text.sqlite"
    build_catalog(source)
    add_ebm_rule_text(source)

    repo = CatalogRepository(source)
    entry = repo.lookup("01210", "2025/Q4")

    assert entry.rule_texts == ["Die Uhrzeit der Inanspruchnahme ist anzugeben."]
    assert entry.description == "Die Uhrzeit der Inanspruchnahme ist anzugeben."


def test_regional_lookup_uses_catalog_metadata(tmp_path):
    source = tmp_path / "catalog_with_regional.sqlite"
    build_catalog(source)
    add_regional_catalog(source)

    repo = CatalogRepository(source)
    entry = repo.lookup_hessen("01210", "2026/Q2")

    assert entry.source == "KV_HESSEN_GOP"
    assert entry.gop == "01210H"
    assert entry.catalog_id == "kv_hessen_gop_2026_q2"
    assert entry.catalog_label == "KV_HESSEN_GOP Hessen 2026/Q2"
    assert entry.data_stand == "01.04.2026"
    assert "Regionaler Zuschlag" in entry.rule_texts
    assert "Nur im regionalen Hessen-Kontext abrechnungsfähig." in entry.rule_texts


def test_regional_catalog_check_reports_matches(tmp_path):
    source = tmp_path / "catalog_with_regional.sqlite"
    build_catalog(source)
    add_regional_catalog(source)

    repo = CatalogRepository(source)
    check = repo.regional_catalog_check(["01210", "06333"], "2026/Q2")

    assert check["checked"] is True
    assert check["catalogs"][0]["catalog_id"] == "kv_hessen_gop_2026_q2"
    assert check["matched_gop_bases"] == ["01210"]
    assert check["matched_gops"][0]["gop"] == "01210H"
    assert check["missing_gop_bases"] == ["06333"]
    assert "regionale Treffer" in check["message"]


def test_regional_catalog_check_reports_no_matching_regional_gops(tmp_path):
    source = tmp_path / "catalog_with_regional.sqlite"
    build_catalog(source)
    add_regional_catalog(source)

    repo = CatalogRepository(source)
    check = repo.regional_catalog_check(["06333"], "2026/Q2")

    assert check["checked"] is True
    assert check["matched_gops"] == []
    assert check["missing_gop_bases"] == ["06333"]
    assert "keine passenden regionalen GOPs" in check["message"]
