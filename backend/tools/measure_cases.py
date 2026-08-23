"""Messstand: Rechnungsentwürfe gegen freigegebene Rechnungen prüfen.

Bisher wurde jede Änderung an einem einzelnen Fall beurteilt, und jede Beurteilung
war eine Wette. Dieses Werkzeug beziffert einen Stand über mehrere Fälle und macht
zwei Stände vergleichbar.

Erwartete Verzeichnisstruktur, bewusst ausserhalb des Repositories:

    faelle/
      <fallname>/
        akte.pdf
        erwartet.json

`erwartet.json` beschreibt die freigegebene Rechnung:

    {
      "quartal": "2026/Q1",
      "region": "Hessen",
      "positionen": [
        {"gop": "01212", "datum": "2026-01-01"},
        {"gop": "01786", "datum": "2026-01-01"}
      ]
    }

Aufruf:

    python -m tools.measure_cases --faelle ../faelle --katalog /pfad/ebm_kbv.sqlite
    python -m tools.measure_cases --faelle ../faelle --bericht stand.json
    python -m tools.measure_cases --faelle ../faelle --vergleich stand.json

Ohne `MISTRAL_API_KEY` schlaegt die semantische Herleitung fehl. Das Werkzeug
bricht dann nicht ab, sondern weist den Fall als "keine Ableitung" mit Grund aus -
der Messstand ist damit auch ohne Modellzugang benutzbar, er misst nur noch nicht
die Zuordnung.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.billing_events import build_billing_events  # noqa: E402
from app.billing_rule_store import get_runtime_clinical_definition_set  # noqa: E402
from app.catalog import CatalogRepository, normalize_gop  # noqa: E402
from app.config import Settings  # noqa: E402
from app.document_segmentation import segment_pages  # noqa: E402
from app.evidence_extraction import extract_evidence  # noqa: E402
from app.invoice_timeline import build_invoice_timeline  # noqa: E402
from app.pdf_text import extract_pages  # noqa: E402
from app.rule_engine import generate_billing_items  # noqa: E402
from app.semantic_billing import SemanticBillingError, generate_semantic_billing_items  # noqa: E402


@dataclass
class CaseResult:
    """Ergebnis eines Falls. Enthaelt keine Patientendaten, nur GOPs und Zahlen."""

    name: str
    quarter: str | None = None
    derived: bool = False
    reason: str | None = None
    expected: list[str] = field(default_factory=list)
    produced: list[str] = field(default_factory=list)
    hit: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    extra: list[str] = field(default_factory=list)
    without_evidence: int = 0
    review_count: int = 0
    amount_expected: float | None = None
    amount_produced: float | None = None

    @property
    def recall(self) -> float:
        return len(self.hit) / len(self.expected) if self.expected else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "fall": self.name,
            "quartal": self.quarter,
            "abgeleitet": self.derived,
            "grund": self.reason,
            "soll": len(self.expected),
            "treffer": len(self.hit),
            "fehlend": self.missing,
            "zusaetzlich": self.extra,
            "ohne_belegstelle": self.without_evidence,
            "review": self.review_count,
            "betrag_soll": self.amount_expected,
            "betrag_ist": self.amount_produced,
        }


def _key(gop: str, date: str | None) -> str:
    """Vergleichsschluessel. Der Suffix bleibt erhalten, weil 32035A und 32035
    fachlich unterschiedliche Angaben auf der Rechnung sind."""
    return f"{str(gop).strip().upper()}@{(date or '')[:10]}"


def _settings(catalog: Path) -> Settings:
    return Settings(
        app_env="measure",
        log_level="info",
        catalog_db_path=catalog,
        storage_dir=Path("/tmp"),
        admin_token=None,
        enable_mistral_ocr=False,
        enable_semantic_billing=bool(os.getenv("MISTRAL_API_KEY")),
        mistral_api_key=os.getenv("MISTRAL_API_KEY") or None,
        mistral_ocr_model=os.getenv("MISTRAL_OCR_MODEL", "mistral-ocr-latest"),
        mistral_llm_model=os.getenv("MISTRAL_LLM_MODEL", "mistral-large-latest"),
    )


def measure_case(directory: Path, catalog_path: Path) -> CaseResult:
    result = CaseResult(name=directory.name)
    pdf = next(iter(sorted(directory.glob("*.pdf"))), None)
    expectation_file = directory / "erwartet.json"
    if pdf is None or not expectation_file.exists():
        result.reason = "akte.pdf oder erwartet.json fehlt"
        return result

    expectation = json.loads(expectation_file.read_text(encoding="utf-8"))
    positions = expectation.get("positionen") or []
    result.expected = [_key(p["gop"], p.get("datum")) for p in positions]
    result.amount_expected = expectation.get("betrag")

    settings = _settings(catalog_path)
    definitions = get_runtime_clinical_definition_set()
    pages, warnings = extract_pages(pdf, settings, definitions)
    segments = segment_pages(pages, definitions)
    evidence, review, _excluded, context = extract_evidence(pages, segments, definitions)
    quarter = expectation.get("quartal") or context.get("quarter")
    region = expectation.get("region") or "Hessen"
    result.quarter = quarter
    result.review_count = len(review)

    catalog = CatalogRepository(catalog_path)
    try:
        semantic = generate_semantic_billing_items(evidence, catalog, quarter, settings, region)
        items, summary = semantic.items, semantic.summary
        result.review_count += len(semantic.review_candidates)
        result.derived = True
    except SemanticBillingError as exc:
        # Ohne Modellzugang oder bei unbrauchbarer Antwort bleibt der deterministische
        # Pfad, der keine Zuordnung leisten kann. Das ist kein Abbruch, sondern ein Befund.
        items, summary = generate_billing_items(evidence, catalog, quarter, region)
        result.reason = str(exc)
    except Exception as exc:  # pragma: no cover - Messwerkzeug soll nie abbrechen
        result.reason = f"unerwarteter Fehler: {exc}"
        return result

    # Zeitleiste wird mitgebaut, weil ein leerer Entwurf ohne sie nicht interpretierbar ist.
    build_invoice_timeline(build_billing_events(evidence, quarter, region), items, quarter, region)

    result.produced = [_key(item.gop_original, item.service_date) for item in items]
    result.amount_produced = summary.amount_total_eur
    expected_set, produced_set = set(result.expected), set(result.produced)
    result.hit = sorted(expected_set & produced_set)
    result.missing = sorted(expected_set - produced_set)
    result.extra = sorted(produced_set - expected_set)
    result.without_evidence = sum(1 for item in items if not item.evidence_ids)
    if warnings:
        result.reason = "; ".join(warnings)[:300] if not result.reason else result.reason
    return result


def measure_all(cases_dir: Path, catalog_path: Path) -> list[CaseResult]:
    directories = sorted(d for d in cases_dir.iterdir() if d.is_dir())
    if not directories:
        raise SystemExit(f"Keine Fallverzeichnisse in {cases_dir}")
    return [measure_case(directory, catalog_path) for directory in directories]


def _totals(results: list[CaseResult]) -> dict[str, Any]:
    expected = sum(len(r.expected) for r in results)
    hit = sum(len(r.hit) for r in results)
    extra = sum(len(r.extra) for r in results)
    return {
        "faelle": len(results),
        "abgeleitet": sum(1 for r in results if r.derived),
        "soll": expected,
        "treffer": hit,
        "fehlend": expected - hit,
        "zusaetzlich": extra,
        "ohne_belegstelle": sum(r.without_evidence for r in results),
        # Die massgebliche Kennzahl: Anteil der Sollpositionen, die ohne Korrektur entstehen.
        "trefferquote": round(hit / expected, 4) if expected else 0.0,
    }


def _print_report(results: list[CaseResult], baseline: dict[str, Any] | None) -> None:
    print(f"{'Fall':<26} {'Quartal':<9} {'Soll':>5} {'Treffer':>8} {'Extra':>6}  Befund")
    print("-" * 92)
    for r in results:
        note = "" if r.derived else (r.reason or "keine Ableitung")[:34]
        if r.derived and r.missing:
            note = "fehlt: " + ", ".join(m.split("@")[0] for m in r.missing[:3])
        print(
            f"{r.name[:24]:<26} {(r.quarter or '-'):<9} {len(r.expected):>5} "
            f"{len(r.hit):>8} {len(r.extra):>6}  {note}"
        )
    totals = _totals(results)
    print("-" * 92)
    print(
        f"{'gesamt':<26} {'':<9} {totals['soll']:>5} {totals['treffer']:>8} "
        f"{totals['zusaetzlich']:>6}  Trefferquote {totals['trefferquote']:.0%}"
    )
    if totals["abgeleitet"] < totals["faelle"]:
        print(f"\n  {totals['faelle'] - totals['abgeleitet']} Fälle ohne Ableitung — Grund je Fall oben.")

    if baseline:
        previous = baseline.get("gesamt", {})
        print("\nVergleich mit dem vorherigen Stand:")
        for label, key in (("Treffer", "treffer"), ("Zusätzlich", "zusaetzlich"), ("Ohne Belegstelle", "ohne_belegstelle")):
            before, now = previous.get(key, 0), totals[key]
            delta = now - before
            sign = "+" if delta > 0 else ""
            print(f"  {label:<18} {before:>5} → {now:<5} {sign}{delta}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--faelle", required=True, type=Path, help="Verzeichnis mit Fallordnern")
    parser.add_argument("--katalog", type=Path, default=Path(os.getenv("CATALOG_DB_PATH", "ebm_kbv.sqlite")))
    parser.add_argument("--bericht", type=Path, help="Ergebnis als JSON schreiben")
    parser.add_argument("--vergleich", type=Path, help="Früherer Bericht als Vergleichsstand")
    args = parser.parse_args(argv)

    if not args.katalog.exists():
        raise SystemExit(f"Katalogdatenbank nicht gefunden: {args.katalog}")
    os.environ.setdefault("CATALOG_DB_PATH", str(args.katalog))

    baseline = json.loads(args.vergleich.read_text(encoding="utf-8")) if args.vergleich else None
    results = measure_all(args.faelle, args.katalog)
    _print_report(results, baseline)

    if args.bericht:
        args.bericht.write_text(
            json.dumps({"faelle": [r.as_dict() for r in results], "gesamt": _totals(results)},
                       ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"\nBericht geschrieben: {args.bericht}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
