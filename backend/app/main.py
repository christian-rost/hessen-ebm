from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .catalog import CatalogRepository
from .config import get_settings
from .document_segmentation import segment_pages
from .evidence_extraction import extract_evidence
from .invoice_export import load_analysis, save_upload, sha256_file, store_analysis
from .models import AnalysisResult
from .pdf_text import extract_pages
from .rule_engine import active_rules_payload, generate_billing_items

app = FastAPI(title="hessen-ebm", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _catalog() -> CatalogRepository:
    return CatalogRepository(get_settings().catalog_db_path)


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "ok": True,
        "app": "hessen-ebm",
        "catalog_available": settings.catalog_db_path.exists(),
        "catalog_db_path": str(settings.catalog_db_path),
    }


@app.get("/api/catalog/status")
def catalog_status() -> dict[str, object]:
    return _catalog().status()


@app.get("/api/catalog/search")
def catalog_search(
    q: str = Query(..., min_length=2),
    quarter: str = "2025/Q4",
    limit: int = Query(25, ge=1, le=100),
) -> dict[str, object]:
    return {
        "query": q,
        "quarter": quarter,
        "results": [entry.model_dump() for entry in _catalog().search(q, quarter, limit)],
    }


@app.get("/api/rules")
def rules() -> dict[str, object]:
    return {"rules": active_rules_payload()}


@app.post("/api/documents/analyze", response_model=AnalysisResult)
async def analyze_document(file: UploadFile = File(...)) -> AnalysisResult:
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Only PDF uploads are supported.")

    settings = get_settings()
    upload_dir = settings.storage_dir / "uploads"
    analysis_dir = settings.storage_dir / "analyses"
    uploaded_path = await save_upload(file, upload_dir)

    pages, warnings = extract_pages(uploaded_path, settings)
    segments = segment_pages(pages)
    evidence, review_candidates, excluded, case_context = extract_evidence(pages, segments)

    catalog = _catalog()
    default_quarter = case_context.get("quarter") or "2025/Q4"
    items, summary = generate_billing_items(evidence, catalog, default_quarter=default_quarter)

    catalog_context = catalog.status()
    catalog_context["analysis_warnings"] = warnings
    catalog_context["case_context"] = case_context

    result = AnalysisResult(
        analysis_id=uuid4().hex,
        status="draft_needs_human_review",
        source_filename=file.filename or uploaded_path.name,
        source_sha256=sha256_file(uploaded_path),
        catalog_context=catalog_context,
        pages=pages,
        segments=segments,
        evidence=evidence,
        items=items,
        review_candidates=review_candidates,
        excluded_evidence=excluded,
        summary=summary,
    )
    store_analysis(result, analysis_dir)
    return result


@app.get("/api/analyses/{analysis_id}", response_model=AnalysisResult)
def get_analysis(analysis_id: str) -> AnalysisResult:
    result = load_analysis(analysis_id, get_settings().storage_dir / "analyses")
    if not result:
        raise HTTPException(status_code=404, detail="Analysis not found.")
    return result

