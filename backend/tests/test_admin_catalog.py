import sqlite3
from pathlib import Path

from app.admin_catalog import install_catalog_database, validate_catalog_database


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
