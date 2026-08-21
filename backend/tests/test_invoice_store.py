import json

from app.invoice_export import store_analysis
from app.invoice_store import build_invoice_item_rows, build_invoice_row, delete_local_invoice, list_local_invoices
from app.models import AnalysisResult, BillingItem, InvoiceSummary


def analysis_result() -> AnalysisResult:
    return AnalysisResult(
        analysis_id="analysis-1",
        status="draft_needs_human_review",
        source_filename="fall.pdf",
        source_sha256="abc123",
        catalog_context={
            "case_context": {
                "quarter": "2026/Q2",
                "treatment_start": "2026-04-24T12:20:00",
                "treatment_end": "2026-04-24T16:30:00",
                "region": "Hessen",
                "diagnosis": "H43.1",
            }
        },
        pages=[],
        segments=[],
        evidence=[],
        items=[
            BillingItem(
                line=1,
                gop_original="06333",
                gop_base="06333",
                title="Binokulare Untersuchung des Augenhintergrundes",
                catalog_source="EBM_KBV",
                catalog_source_label="KBV EBM 2026/Q2",
                catalog_id="ebm_kbv_2026_q2",
                catalog_data_stand="02.04.2026",
                quarter="2026/Q2",
                service_date="2026-04-24",
                quantity=1,
                points=53,
                amount_eur=6.75,
                rule_id="semantic.llm",
                confidence="high",
                evidence_ids=["ev-1"],
                evidence_pages=[3],
                derivation_source="semantic_llm",
                semantic_reason="Dokumentierte Augenhintergrunduntersuchung.",
            )
        ],
        review_candidates=[],
        excluded_evidence=[],
        summary=InvoiceSummary(
            line_count=1,
            points_total=53,
            amount_total_eur=6.75,
            human_review_required=True,
        ),
    )


def test_build_invoice_row_contains_retrieval_metadata():
    row = build_invoice_row(analysis_result())

    assert row["analysis_id"] == "analysis-1"
    assert row["quarter"] == "2026/Q2"
    assert row["diagnosis"] == "H43.1"
    assert row["line_count"] == 1
    assert row["amount_total_eur"] == 6.75
    assert row["payload"]["items"][0]["gop_original"] == "06333"


def test_build_invoice_item_rows_store_positions_separately():
    rows = build_invoice_item_rows(analysis_result())

    assert rows == [
        {
            "analysis_id": "analysis-1",
            "line": 1,
            "gop_original": "06333",
            "gop_base": "06333",
            "gop_suffix": None,
            "title": "Binokulare Untersuchung des Augenhintergrundes",
            "catalog_source": "EBM_KBV",
            "catalog_source_label": "KBV EBM 2026/Q2",
            "catalog_id": "ebm_kbv_2026_q2",
            "catalog_data_stand": "02.04.2026",
            "quarter": "2026/Q2",
            "service_date": "2026-04-24",
            "service_time": None,
            "quantity": 1,
            "points": 53,
            "amount_eur": 6.75,
            "rule_id": "semantic.llm",
            "confidence": "high",
            "evidence_ids": ["ev-1"],
            "evidence_pages": [3],
            "validation_status": "valid",
            "validation_notes": [],
            "derivation_source": "semantic_llm",
            "semantic_reason": "Dokumentierte Augenhintergrunduntersuchung.",
            "semantic_catalog_candidates": [],
            "payload": rows[0]["payload"],
        }
    ]


def test_supabase_rows_remove_null_characters_recursively():
    result = analysis_result()
    result.source_filename = "fall\x00.pdf"
    result.catalog_context["case_context"]["diagnosis"] = "H43\x00.1"
    result.catalog_context["ocr"] = {
        "raw\x00key": "Text vor\x00Text nach",
        "nested": ["Wert\x00", {"text": "\x00Befund"}],
    }
    result.items[0].semantic_reason = "Dokumentierte\x00 Untersuchung."

    invoice_row = build_invoice_row(result)
    item_rows = build_invoice_item_rows(result)

    serialized = json.dumps({"invoice": invoice_row, "items": item_rows}, ensure_ascii=False)
    assert "\x00" not in serialized
    assert invoice_row["source_filename"] == "fall.pdf"
    assert invoice_row["diagnosis"] == "H43.1"
    assert invoice_row["payload"]["catalog_context"]["ocr"]["rawkey"] == "Text vorText nach"
    assert item_rows[0]["semantic_reason"] == "Dokumentierte Untersuchung."
    assert item_rows[0]["payload"]["semantic_reason"] == "Dokumentierte Untersuchung."


def test_list_local_invoices_keeps_json_fallback_available(tmp_path):
    store_analysis(analysis_result(), tmp_path)

    listing = list_local_invoices(tmp_path)

    assert listing["storage_backend"] == "local_json"
    assert listing["total"] == 1
    assert listing["items"][0]["analysis_id"] == "analysis-1"
    assert listing["items"][0]["line_count"] == 1


def test_delete_local_invoice_removes_json_fallback(tmp_path):
    store_analysis(analysis_result(), tmp_path)

    assert delete_local_invoice("analysis-1", tmp_path) is True
    assert list_local_invoices(tmp_path)["total"] == 0
    assert delete_local_invoice("analysis-1", tmp_path) is False
