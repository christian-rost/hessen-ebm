#!/usr/bin/env python3
"""Scrape the KBV EBM browser into a local SQLite database.

The site at https://ebm.kbv.de/ is a JSF/PrimeFaces application. This script
uses the same AJAX calls as the tree widget:

* mainForm:tree_expandNode to discover child nodes
* mainForm:tree_instantSelection with the select behavior to fetch details

The default target is the currently selected quarter on the website. Pass
--quarter to archive a specific historical quarter in the same database.
"""

from __future__ import annotations

import argparse
import datetime as dt
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import requests
from lxml import etree, html


BASE_URL = "https://ebm.kbv.de/"
AJAX_HEADERS = {
    "Faces-Request": "partial/ajax",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": BASE_URL,
    "Origin": BASE_URL,
}


@dataclass
class Node:
    row_key: str
    parent_row_key: Optional[str]
    sort_index: int
    level: int
    label: str
    is_parent: bool
    is_leaf: bool


@dataclass
class Detail:
    row_key: str
    knoten_id: str
    gop: str
    title: str
    points: str
    euro: str
    flags: List[str]
    html: str
    text: str


class EbmScraper:
    def __init__(self, delay: float = 0.05, timeout: int = 30) -> None:
        self.session = requests.Session()
        self.delay = delay
        self.timeout = timeout
        self.view_state = ""
        self.initial_html = ""
        self.header_form_html = ""
        self.main_form_html = ""
        self.selected_quarter = ""

    def initialize(self) -> None:
        response = self.session.get(BASE_URL, timeout=self.timeout)
        response.raise_for_status()
        self.initial_html = response.text
        self.header_form_html = self._extract_header_form(self.initial_html)
        self.main_form_html = self._extract_main_form(self.initial_html)
        self.view_state = self._extract_main_view_state(self.initial_html)
        self.selected_quarter = self._selected_quarter_from_html(self.header_form_html)

    def select_quarter(self, quarter: str) -> None:
        available_quarters = {value for value, _selected in self.quarters()}
        if quarter not in available_quarters:
            available = ", ".join(sorted(available_quarters, reverse=True))
            raise ValueError(f"Quarter {quarter!r} is not available. Available: {available}")
        if quarter == self.selected_quarter:
            return

        response = self._ajax(
            source="headerForm:quartalCombobox",
            execute="headerForm",
            render="headerForm:quartalChooserPG mainForm",
            form_name="headerForm",
            extra={
                "headerForm:quartalCombobox": quarter,
                "javax.faces.behavior.event": "valueChange",
                "javax.faces.partial.event": "change",
            },
        )
        quarter_chooser = partial_update(response, "headerForm:quartalChooserPG")
        main_form = partial_update(response, "mainForm")
        if not main_form:
            raise RuntimeError(f"Could not load mainForm for quarter {quarter}")
        if quarter_chooser:
            self.header_form_html = quarter_chooser
        self.main_form_html = main_form
        self.selected_quarter = quarter

    def metadata(self) -> Dict[str, str]:
        doc = parse_html_document(self.initial_html)
        version = first_text(
            doc.xpath('//li[contains(normalize-space(.), "Versionsnummer:")]/text()')
        ).replace("Versionsnummer:", "").strip()
        data_stand = first_text(
            doc.xpath('//li[contains(normalize-space(.), "Datenstand:")]/text()')
        ).replace("Datenstand:", "").strip()
        return {
            "source_url": BASE_URL,
            "selected_quarter": self.selected_quarter,
            "site_version": version,
            "data_stand": data_stand,
            "retrieved_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        }

    def quarters(self) -> List[Tuple[str, int]]:
        doc = parse_html_fragment(self.header_form_html)
        rows = []
        for option in doc.xpath('//select[@id="headerForm:quartalCombobox"]/option'):
            rows.append((clean_text(option.text_content()), 1 if option.get("selected") else 0))
        return rows

    def doctor_groups(self) -> List[str]:
        doc = parse_html_fragment(self.main_form_html)
        rows = []
        for option in doc.xpath('//select[@id="mainForm:arztgrpFilter"]/option'):
            value = clean_text(option.text_content())
            if value and "Kein Filter" not in value:
                rows.append(value)
        return rows

    def root_nodes(self) -> List[Node]:
        return parse_nodes(self.main_form_html, None)

    def expand_node(self, row_key: str) -> List[Node]:
        response = self._ajax(
            source="mainForm:tree",
            execute="mainForm:tree",
            render="mainForm:tree",
            extra={"mainForm:tree_expandNode": row_key},
        )
        fragment = partial_update(response, "mainForm:tree")
        return parse_nodes(fragment, row_key)

    def select_detail(self, row_key: str) -> Optional[Detail]:
        response = self._ajax(
            source="mainForm:tree",
            execute="mainForm:tree",
            render="mainForm:detailsPanel mainForm:buttonsPG",
            extra={
                "mainForm:tree_instantSelection": row_key,
                "mainForm:tree_selection": row_key,
                "javax.faces.behavior.event": "select",
                "javax.faces.partial.event": "select",
            },
        )
        fragment = partial_update(response, "mainForm:detailsPanel")
        if not fragment:
            return None
        return parse_detail(row_key, fragment)

    def _ajax(
        self,
        source: str,
        execute: str,
        render: str,
        extra: Dict[str, str],
        form_name: str = "mainForm",
    ) -> str:
        data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": source,
            "javax.faces.partial.execute": execute,
            "javax.faces.partial.render": render,
            "javax.faces.ViewState": self.view_state,
        }
        if form_name == "mainForm":
            data.update(
                {
                    "mainForm": "mainForm",
                    "mainForm:arztgrpFilter": "",
                    "mainForm:searchTerm_input": "",
                    "mainForm:searchTerm_hinput": "",
                    "mainForm:tree_selection": "",
                    "mainForm:tree_scrollState": "0,0",
                }
            )
        elif form_name == "headerForm":
            data["headerForm"] = "headerForm"
        else:
            raise ValueError(f"Unsupported JSF form: {form_name}")
        data.update(extra)

        last_error: Optional[BaseException] = None
        for attempt in range(4):
            try:
                response = self.session.post(
                    BASE_URL,
                    data=data,
                    headers=AJAX_HEADERS,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                self.view_state = update_view_state(response.text, self.view_state)
                if self.delay:
                    time.sleep(self.delay)
                return response.text
            except (requests.RequestException, etree.XMLSyntaxError) as exc:
                last_error = exc
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"AJAX request failed for {extra}") from last_error

    @staticmethod
    def _extract_main_form(page: str) -> str:
        match = re.search(r'<form id="mainForm".*?</form>', page, re.S)
        if not match:
            raise RuntimeError("Could not find mainForm in initial page")
        return match.group(0)

    @staticmethod
    def _extract_header_form(page: str) -> str:
        match = re.search(r'<form id="headerForm".*?</form>', page, re.S)
        if not match:
            raise RuntimeError("Could not find headerForm in initial page")
        return match.group(0)

    @classmethod
    def _extract_main_view_state(cls, page: str) -> str:
        form = cls._extract_main_form(page)
        matches = re.findall(
            r'name="javax\.faces\.ViewState"[^>]*value="([^"]+)"',
            form,
        )
        if not matches:
            raise RuntimeError("Could not find javax.faces.ViewState in mainForm")
        return matches[-1]

    @staticmethod
    def _selected_quarter_from_html(fragment: str) -> str:
        doc = parse_html_fragment(fragment)
        return first_text(
            doc.xpath('//select[@id="headerForm:quartalCombobox"]/option[@selected]/text()')
        )


class EbmDatabase:
    def __init__(self, path: str) -> None:
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=DELETE")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.ensure_schema()

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()

    def ensure_schema(self) -> None:
        self._migrate_legacy_schema_if_needed()
        self._create_versioned_schema()
        self.conn.commit()

    def _create_versioned_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS snapshots (
                quarter TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                site_version TEXT,
                data_stand TEXT,
                retrieved_at TEXT NOT NULL,
                node_count INTEGER NOT NULL DEFAULT 0,
                detail_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS quarters (
                quarter TEXT PRIMARY KEY,
                selected INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS doctor_groups (
                quarter TEXT NOT NULL REFERENCES snapshots(quarter) ON DELETE CASCADE,
                name TEXT NOT NULL,
                PRIMARY KEY (quarter, name)
            );

            CREATE TABLE IF NOT EXISTS nodes (
                quarter TEXT NOT NULL REFERENCES snapshots(quarter) ON DELETE CASCADE,
                row_key TEXT NOT NULL,
                parent_row_key TEXT,
                sort_index INTEGER NOT NULL,
                level INTEGER NOT NULL,
                label TEXT NOT NULL,
                is_parent INTEGER NOT NULL,
                is_leaf INTEGER NOT NULL,
                PRIMARY KEY (quarter, row_key),
                FOREIGN KEY (quarter, parent_row_key) REFERENCES nodes(quarter, row_key)
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_parent
                ON nodes(quarter, parent_row_key, sort_index);

            CREATE INDEX IF NOT EXISTS idx_nodes_label
                ON nodes(quarter, label);

            CREATE TABLE IF NOT EXISTS details (
                quarter TEXT NOT NULL,
                row_key TEXT NOT NULL,
                knoten_id TEXT,
                gop TEXT,
                title TEXT,
                points TEXT,
                euro TEXT,
                flags TEXT,
                html TEXT,
                text TEXT,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (quarter, row_key),
                FOREIGN KEY (quarter, row_key) REFERENCES nodes(quarter, row_key) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_details_gop
                ON details(quarter, gop);

            CREATE TABLE IF NOT EXISTS detail_sections (
                quarter TEXT NOT NULL,
                row_key TEXT NOT NULL,
                section_index INTEGER NOT NULL,
                section_id TEXT,
                heading TEXT,
                html TEXT,
                text TEXT,
                PRIMARY KEY (quarter, row_key, section_index),
                FOREIGN KEY (quarter, row_key) REFERENCES details(quarter, row_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS detail_tables (
                quarter TEXT NOT NULL,
                row_key TEXT NOT NULL,
                table_index INTEGER NOT NULL,
                section_id TEXT,
                html TEXT,
                text TEXT,
                PRIMARY KEY (quarter, row_key, table_index),
                FOREIGN KEY (quarter, row_key) REFERENCES details(quarter, row_key) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS detail_links (
                quarter TEXT NOT NULL,
                row_key TEXT NOT NULL,
                link_index INTEGER NOT NULL,
                href TEXT,
                label TEXT,
                context TEXT,
                PRIMARY KEY (quarter, row_key, link_index),
                FOREIGN KEY (quarter, row_key) REFERENCES details(quarter, row_key) ON DELETE CASCADE
            );
            """
        )
        try:
            self.conn.execute(
                """
                CREATE VIRTUAL TABLE IF NOT EXISTS search
                USING fts5(quarter UNINDEXED, row_key UNINDEXED, label, gop, title, content)
                """
            )
        except sqlite3.DatabaseError:
            # Some SQLite builds omit FTS5. The normalized tables still work.
            pass

    def _migrate_legacy_schema_if_needed(self) -> None:
        if not self._table_exists("nodes"):
            return
        if "quarter" in self._table_columns("nodes"):
            return

        legacy_metadata = self._legacy_metadata()
        legacy_quarter = (
            legacy_metadata.get("selected_quarter")
            or self._legacy_selected_quarter()
            or "unknown"
        )
        source_url = legacy_metadata.get("source_url", BASE_URL)
        site_version = legacy_metadata.get("site_version", "")
        data_stand = legacy_metadata.get("data_stand", "")
        retrieved_at = legacy_metadata.get(
            "retrieved_at",
            dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        )

        legacy_tables = [
            "metadata",
            "quarters",
            "doctor_groups",
            "nodes",
            "details",
            "detail_sections",
            "detail_tables",
            "detail_links",
        ]

        self.conn.execute("PRAGMA foreign_keys=OFF")
        self.conn.execute("DROP TABLE IF EXISTS search")
        for table in legacy_tables:
            if self._table_exists(table):
                legacy_table = f"__legacy_{table}"
                self.conn.execute(f"DROP TABLE IF EXISTS {legacy_table}")
                self.conn.execute(f"ALTER TABLE {table} RENAME TO {legacy_table}")

        self._create_versioned_schema()
        self.conn.execute(
            """
            INSERT OR REPLACE INTO snapshots(
                quarter, source_url, site_version, data_stand, retrieved_at, node_count, detail_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                legacy_quarter,
                source_url,
                site_version,
                data_stand,
                retrieved_at,
                self._legacy_count("__legacy_nodes"),
                self._legacy_count("__legacy_details"),
            ),
        )

        if self._table_exists("__legacy_metadata"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO metadata(key, value)
                SELECT key, value FROM __legacy_metadata
                """
            )
        self.conn.execute(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            ("last_scrape_quarter", legacy_quarter),
        )

        if self._table_exists("__legacy_quarters"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO quarters(quarter, selected)
                SELECT quarter, selected FROM __legacy_quarters
                """
            )
        self.conn.execute(
            "INSERT OR REPLACE INTO quarters(quarter, selected) VALUES (?, 1)",
            (legacy_quarter,),
        )

        if self._table_exists("__legacy_doctor_groups"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO doctor_groups(quarter, name)
                SELECT ?, name FROM __legacy_doctor_groups
                """,
                (legacy_quarter,),
            )
        if self._table_exists("__legacy_nodes"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO nodes(
                    quarter, row_key, parent_row_key, sort_index, level, label, is_parent, is_leaf
                )
                SELECT ?, row_key, parent_row_key, sort_index, level, label, is_parent, is_leaf
                FROM __legacy_nodes
                """,
                (legacy_quarter,),
            )
        if self._table_exists("__legacy_details"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO details(
                    quarter, row_key, knoten_id, gop, title, points, euro, flags, html, text, fetched_at
                )
                SELECT ?, row_key, knoten_id, gop, title, points, euro, flags, html, text, fetched_at
                FROM __legacy_details
                """,
                (legacy_quarter,),
            )
        if self._table_exists("__legacy_detail_sections"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO detail_sections(
                    quarter, row_key, section_index, section_id, heading, html, text
                )
                SELECT ?, row_key, section_index, section_id, heading, html, text
                FROM __legacy_detail_sections
                """,
                (legacy_quarter,),
            )
        if self._table_exists("__legacy_detail_tables"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO detail_tables(
                    quarter, row_key, table_index, section_id, html, text
                )
                SELECT ?, row_key, table_index, section_id, html, text
                FROM __legacy_detail_tables
                """,
                (legacy_quarter,),
            )
        if self._table_exists("__legacy_detail_links"):
            self.conn.execute(
                """
                INSERT OR REPLACE INTO detail_links(
                    quarter, row_key, link_index, href, label, context
                )
                SELECT ?, row_key, link_index, href, label, context
                FROM __legacy_detail_links
                """,
                (legacy_quarter,),
            )
        if self._has_search_table() and self._table_exists("__legacy_details"):
            self.conn.execute(
                """
                INSERT INTO search(quarter, row_key, label, gop, title, content)
                SELECT ?, d.row_key, COALESCE(n.label, ''), d.gop, d.title, d.text
                FROM __legacy_details d
                LEFT JOIN __legacy_nodes n ON n.row_key = d.row_key
                """,
                (legacy_quarter,),
            )

        for table in reversed(legacy_tables):
            legacy_table = f"__legacy_{table}"
            if self._table_exists(legacy_table):
                self.conn.execute(f"DROP TABLE {legacy_table}")
        self.conn.commit()
        self.conn.execute("PRAGMA foreign_keys=ON")

    def reset_content(self) -> None:
        self.conn.executescript(
            """
            DELETE FROM detail_links;
            DELETE FROM detail_tables;
            DELETE FROM detail_sections;
            DELETE FROM details;
            DELETE FROM nodes;
            DELETE FROM doctor_groups;
            DELETE FROM snapshots;
            DELETE FROM quarters;
            DELETE FROM metadata;
            """
        )
        if self._has_search_table():
            self.conn.execute("DELETE FROM search")
        self.conn.commit()

    def delete_quarter(self, quarter: str) -> None:
        if self._has_search_table():
            self.conn.execute("DELETE FROM search WHERE quarter = ?", (quarter,))
        self.conn.execute("DELETE FROM detail_links WHERE quarter = ?", (quarter,))
        self.conn.execute("DELETE FROM detail_tables WHERE quarter = ?", (quarter,))
        self.conn.execute("DELETE FROM detail_sections WHERE quarter = ?", (quarter,))
        self.conn.execute("DELETE FROM details WHERE quarter = ?", (quarter,))
        self.conn.execute("DELETE FROM nodes WHERE quarter = ?", (quarter,))
        self.conn.execute("DELETE FROM doctor_groups WHERE quarter = ?", (quarter,))
        self.conn.execute("DELETE FROM snapshots WHERE quarter = ?", (quarter,))
        self.conn.commit()

    def upsert_metadata(self, metadata: Dict[str, str]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
            sorted(metadata.items()),
        )

    def upsert_snapshot(self, metadata: Dict[str, str], node_count: int = 0, detail_count: int = 0) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO snapshots(
                quarter, source_url, site_version, data_stand, retrieved_at, node_count, detail_count
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                metadata["selected_quarter"],
                metadata.get("source_url", BASE_URL),
                metadata.get("site_version", ""),
                metadata.get("data_stand", ""),
                metadata.get("retrieved_at", ""),
                node_count,
                detail_count,
            ),
        )

    def update_snapshot_counts(self, quarter: str, node_count: int, detail_count: int) -> None:
        self.conn.execute(
            """
            UPDATE snapshots
            SET node_count = ?, detail_count = ?
            WHERE quarter = ?
            """,
            (node_count, detail_count, quarter),
        )

    def upsert_quarters(self, quarters: Iterable[Tuple[str, int]]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO quarters(quarter, selected) VALUES (?, ?)",
            quarters,
        )

    def upsert_doctor_groups(self, quarter: str, groups: Iterable[str]) -> None:
        self.conn.executemany(
            "INSERT OR REPLACE INTO doctor_groups(quarter, name) VALUES (?, ?)",
            [(quarter, group) for group in groups],
        )

    def upsert_nodes(self, quarter: str, nodes: Iterable[Node]) -> None:
        self.conn.executemany(
            """
            INSERT OR REPLACE INTO nodes(
                quarter, row_key, parent_row_key, sort_index, level, label, is_parent, is_leaf
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    quarter,
                    node.row_key,
                    node.parent_row_key,
                    node.sort_index,
                    node.level,
                    node.label,
                    int(node.is_parent),
                    int(node.is_leaf),
                )
                for node in nodes
            ],
        )

    def upsert_detail(self, quarter: str, node: Node, detail: Detail) -> None:
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.conn.execute(
            """
            INSERT OR REPLACE INTO details(
                quarter, row_key, knoten_id, gop, title, points, euro, flags, html, text, fetched_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                quarter,
                detail.row_key,
                detail.knoten_id,
                detail.gop,
                detail.title,
                detail.points,
                detail.euro,
                "\n".join(detail.flags),
                detail.html,
                detail.text,
                now,
            ),
        )

        sections, tables, links = parse_detail_children(quarter, detail.row_key, detail.html)
        self.conn.execute(
            "DELETE FROM detail_sections WHERE quarter = ? AND row_key = ?",
            (quarter, detail.row_key),
        )
        self.conn.execute(
            "DELETE FROM detail_tables WHERE quarter = ? AND row_key = ?",
            (quarter, detail.row_key),
        )
        self.conn.execute(
            "DELETE FROM detail_links WHERE quarter = ? AND row_key = ?",
            (quarter, detail.row_key),
        )
        self.conn.executemany(
            """
            INSERT INTO detail_sections(
                quarter, row_key, section_index, section_id, heading, html, text
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            sections,
        )
        self.conn.executemany(
            """
            INSERT INTO detail_tables(quarter, row_key, table_index, section_id, html, text)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            tables,
        )
        self.conn.executemany(
            """
            INSERT INTO detail_links(quarter, row_key, link_index, href, label, context)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            links,
        )

        if self._has_search_table():
            self.conn.execute(
                "DELETE FROM search WHERE quarter = ? AND row_key = ?",
                (quarter, detail.row_key),
            )
            self.conn.execute(
                """
                INSERT INTO search(quarter, row_key, label, gop, title, content)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (quarter, detail.row_key, node.label, detail.gop, detail.title, detail.text),
            )

    def existing_detail_keys(self, quarter: str) -> set:
        return {
            row[0]
            for row in self.conn.execute(
                "SELECT row_key FROM details WHERE quarter = ?",
                (quarter,),
            )
        }

    def detail_count(self, quarter: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM details WHERE quarter = ?",
            (quarter,),
        ).fetchone()[0]

    def _has_search_table(self) -> bool:
        row = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='search'"
        ).fetchone()
        return bool(row)

    def _table_exists(self, table: str) -> bool:
        return bool(
            self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
                (table,),
            ).fetchone()
        )

    def _table_columns(self, table: str) -> set:
        if not self._table_exists(table):
            return set()
        return {row[1] for row in self.conn.execute(f"PRAGMA table_info({table})")}

    def _legacy_metadata(self) -> Dict[str, str]:
        if not self._table_exists("metadata"):
            return {}
        return dict(self.conn.execute("SELECT key, value FROM metadata"))

    def _legacy_selected_quarter(self) -> str:
        if not self._table_exists("quarters"):
            return ""
        row = self.conn.execute(
            "SELECT quarter FROM quarters WHERE selected = 1 LIMIT 1"
        ).fetchone()
        return row[0] if row else ""

    def _legacy_count(self, table: str) -> int:
        if not self._table_exists(table):
            return 0
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def partial_update(response_text: str, update_id: str) -> str:
    doc = etree.fromstring(response_text.encode("utf-8"))
    for update in doc.xpath(".//update"):
        if update.get("id") == update_id:
            return update.text or ""
    return ""


def update_view_state(response_text: str, fallback: str) -> str:
    match = re.search(
        r'<update id="[^"]*javax\.faces\.ViewState[^"]*"><!\[CDATA\[(.*?)\]\]></update>',
        response_text,
        re.S,
    )
    return match.group(1) if match else fallback


def parse_nodes(fragment: str, parent_row_key: Optional[str]) -> List[Node]:
    if not fragment.strip():
        return []
    root = html.fragment_fromstring("<root>" + fragment + "</root>", create_parent=False)
    nodes = []
    for index, li in enumerate(
        root.xpath('.//li[contains(concat(" ", normalize-space(@class), " "), " ui-treenode ")]')
    ):
        row_key = li.get("data-rowkey", "")
        classes = li.get("class", "")
        label = first_text(li.xpath('.//span[contains(@style, "white-space")]/text()'))
        if not label:
            label = first_text(
                li.xpath('.//span[contains(@class, "ui-treenode-label")]//text()')
            )
        nodes.append(
            Node(
                row_key=row_key,
                parent_row_key=parent_row_key,
                sort_index=index,
                level=row_key.count("_"),
                label=clean_text(label),
                is_parent="ui-treenode-parent" in classes,
                is_leaf="ui-treenode-leaf" in classes,
            )
        )
    return nodes


def parse_html_document(document: str) -> html.HtmlElement:
    return html.fromstring(document.encode("utf-8"))


def parse_html_fragment(fragment: str) -> html.HtmlElement:
    return html.fragment_fromstring("<root>" + fragment + "</root>", create_parent=False)


def parse_detail(row_key: str, fragment: str) -> Detail:
    root = html.fragment_fromstring("<root>" + fragment + "</root>", create_parent=False)
    knoten_id = first_text(root.xpath('.//*[@id="knotenId"]/text()'))
    detail_row_key = first_text(root.xpath('.//*[@id="rowKey"]/text()')) or row_key
    gop = first_text(root.xpath('.//h4[@id="gop"]/text()'))

    title = ""
    heading_nodes = root.xpath('.//*[@id="ueberschrift_details"]//h4')
    if heading_nodes:
        heading_texts = [clean_text(node.text_content()) for node in heading_nodes]
        if gop and heading_texts and heading_texts[0] == gop:
            title = " ".join(heading_texts[1:]).strip()
        else:
            title = " ".join(heading_texts).strip()
    if not title:
        title = first_text(root.xpath(".//h3/text() | .//h4/text() | .//h5/text()"))

    values = []
    for block in root.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " col-md-2 ")]'):
        value = first_text(block.xpath('.//*[contains(@class, "value")]/text()'))
        unit = first_text(block.xpath('.//*[contains(@class, "unit")]/text()')).upper()
        if value and unit:
            values.append((unit, value))
    points = first_value(values, "PUNKTE")
    euro = first_value(values, "EURO")
    flags = [
        clean_text(item.text_content())
        for item in root.xpath('.//*[contains(concat(" ", normalize-space(@class), " "), " glyphicon-class ")]')
        if clean_text(item.text_content())
    ]

    cleaned = html.fragment_fromstring("<root>" + fragment + "</root>", create_parent=False)
    remove_noise(cleaned)
    text = clean_text(cleaned.text_content())

    return Detail(
        row_key=detail_row_key,
        knoten_id=clean_text(knoten_id),
        gop=clean_text(gop),
        title=clean_text(title),
        points=clean_text(points),
        euro=clean_text(euro),
        flags=flags,
        html=fragment,
        text=text,
    )


def parse_detail_children(quarter: str, row_key: str, fragment: str) -> Tuple[List[tuple], List[tuple], List[tuple]]:
    root = html.fragment_fromstring("<root>" + fragment + "</root>", create_parent=False)
    remove_noise(root)

    sections = []
    tables = []
    links = []

    details_content = root.xpath('.//*[@id="detailsContent"]')
    parent = details_content[0] if details_content else root
    section_index = 0
    table_index = 0
    link_index = 0

    for element in parent.xpath('./div[not(@id="knotenId") and not(@id="rowKey")]'):
        section_id = element.get("id") or ""
        heading = first_text(element.xpath("./h5/text() | ./h4/text() | ./h3/text()"))
        element_html = html.tostring(element, encoding="unicode", method="html")
        element_text = clean_text(element.text_content())
        if element_text:
            sections.append((quarter, row_key, section_index, section_id, heading, element_html, element_text))
            section_index += 1
        for table in element.xpath(".//table"):
            table_html = html.tostring(table, encoding="unicode", method="html")
            table_text = clean_text(table.text_content())
            tables.append((quarter, row_key, table_index, section_id, table_html, table_text))
            table_index += 1
        for link in element.xpath('.//a[contains(concat(" ", normalize-space(@class), " "), " EBMLINKELEMENT ")]'):
            label = clean_text(link.text_content())
            href = link.get("href", "")
            context = clean_text(link.getparent().text_content()) if link.getparent() is not None else ""
            links.append((quarter, row_key, link_index, href, label, context))
            link_index += 1

    return sections, tables, links


def remove_noise(root: html.HtmlElement) -> None:
    for element in root.xpath(".//script"):
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)
    for element in root.xpath('.//*[@id="knotenId" or @id="rowKey" or @id="mainForm:messages"]'):
        parent = element.getparent()
        if parent is not None:
            parent.remove(element)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def first_text(values: Sequence[str]) -> str:
    for value in values:
        cleaned = clean_text(value)
        if cleaned:
            return cleaned
    return ""


def first_value(values: Sequence[Tuple[str, str]], unit: str) -> str:
    for found_unit, value in values:
        if found_unit == unit:
            return value
    return ""


def scrape(args: argparse.Namespace) -> None:
    scraper = EbmScraper(delay=args.delay, timeout=args.timeout)
    db = EbmDatabase(args.db)
    try:
        scraper.initialize()
        if args.quarter:
            scraper.select_quarter(args.quarter)

        metadata = scraper.metadata()
        quarter = metadata["selected_quarter"]

        if args.replace_quarter and args.resume:
            raise ValueError("--replace-quarter cannot be combined with --resume")
        if args.reset:
            db.reset_content()
        elif args.replace_quarter:
            db.delete_quarter(quarter)

        db.upsert_metadata(
            {
                **metadata,
                "last_scrape_quarter": quarter,
            }
        )
        db.upsert_snapshot(metadata)
        db.upsert_quarters(scraper.quarters())
        db.upsert_doctor_groups(quarter, scraper.doctor_groups())

        root_nodes = scraper.root_nodes()
        nodes_by_key: Dict[str, Node] = {node.row_key: node for node in root_nodes}
        db.upsert_nodes(quarter, root_nodes)

        queue = [node for node in root_nodes if node.is_parent]
        expanded = 0
        while queue:
            node = queue.pop(0)
            children = scraper.expand_node(node.row_key)
            if args.limit_nodes and len(nodes_by_key) >= args.limit_nodes:
                break
            for child in children:
                if child.row_key not in nodes_by_key:
                    nodes_by_key[child.row_key] = child
                    if child.is_parent:
                        queue.append(child)
            db.upsert_nodes(quarter, children)
            expanded += 1
            if expanded % args.commit_every == 0:
                db.conn.commit()
            if expanded % args.progress_every == 0:
                print(
                    f"expanded={expanded} nodes={len(nodes_by_key)} queue={len(queue)}",
                    flush=True,
                )

        db.update_snapshot_counts(quarter, len(nodes_by_key), db.detail_count(quarter))
        db.upsert_metadata({"last_scrape_node_count": str(len(nodes_by_key))})
        db.conn.commit()
        print(
            f"Tree complete for {quarter}: {len(nodes_by_key)} nodes, {expanded} expanded.",
            flush=True,
        )

        if args.no_details:
            return

        existing = db.existing_detail_keys(quarter) if args.resume else set()
        detail_nodes = list(nodes_by_key.values())
        fetched = 0
        skipped = 0
        for node in detail_nodes:
            if node.row_key in existing:
                skipped += 1
                continue
            if args.limit_details and fetched >= args.limit_details:
                break
            detail = scraper.select_detail(node.row_key)
            if detail is not None:
                db.upsert_detail(quarter, node, detail)
                fetched += 1
            if fetched % args.commit_every == 0:
                db.conn.commit()
            if fetched and fetched % args.progress_every == 0:
                print(
                    f"details={fetched} skipped={skipped} total={len(detail_nodes)}",
                    flush=True,
                )

        detail_count = db.detail_count(quarter)
        db.update_snapshot_counts(quarter, len(nodes_by_key), detail_count)
        db.upsert_metadata(
            {
                "last_scrape_detail_count": str(detail_count),
            }
        )
        db.conn.commit()
        print(
            f"Details complete for {quarter}: fetched={fetched}, skipped={skipped}.",
            flush=True,
        )
    finally:
        db.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="ebm_kbv.sqlite", help="SQLite output path")
    parser.add_argument("--quarter", help="Specific quarter to scrape, for example 2026/Q1")
    parser.add_argument("--delay", type=float, default=0.05, help="Delay between AJAX calls")
    parser.add_argument("--timeout", type=int, default=30, help="HTTP timeout in seconds")
    parser.add_argument("--reset", action="store_true", help="Clear all quarters before scraping")
    parser.add_argument("--replace-quarter", action="store_true", help="Delete only the target quarter before scraping")
    parser.add_argument("--resume", action="store_true", help="Skip details already present for the target quarter")
    parser.add_argument("--no-details", action="store_true", help="Only scrape tree structure")
    parser.add_argument("--limit-nodes", type=int, default=0, help="Stop tree scrape after N nodes")
    parser.add_argument("--limit-details", type=int, default=0, help="Fetch at most N detail pages")
    parser.add_argument("--commit-every", type=int, default=50, help="Commit every N operations")
    parser.add_argument("--progress-every", type=int, default=25, help="Print progress every N operations")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        scrape(args)
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
