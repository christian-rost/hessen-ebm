from __future__ import annotations

import re
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware

from .admin_auth import require_admin
from .admin_catalog import CatalogValidationError, install_catalog_database, list_catalog_backups, validate_catalog_database
from .admin_catalog_imports import CatalogImportError, import_regional_catalog_pdf, scrape_ebm_quarter_into_catalog
from .admin_jobs import JobAlreadyRunningError, JobNotFoundError, get_job, running_catalog_job, start_catalog_job
from .admin_rule_compilation import compile_and_migrate_catalog_rules
from .analysis_jobs import AnalysisJobNotFoundError, get_analysis_job, start_analysis_job
from .catalog import CatalogRepository
from .billing_events import build_billing_events, episode_selection_payload
from .billing_rule_store import get_runtime_billing_rule_set, rule_store_status
from .config import Settings, get_settings
from .database import supabase_status
from .document_segmentation import segment_pages
from .evidence_extraction import extract_evidence
from .invoice_export import load_analysis, save_upload, sha256_file, store_analysis
from .invoice_store import delete_invoice, list_invoices, load_invoice, save_invoice
from .models import AnalysisResult, ReviewCandidate
from .pdf_text import extract_pages
from .rule_engine import generate_billing_items, rule_overview_payload
from .semantic_billing import SemanticBillingError, generate_semantic_billing_items

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
        "supabase": supabase_status(),
        "billing_rules": rule_store_status(),
    }


@app.get("/api/catalog/status")
def catalog_status() -> dict[str, object]:
    return _catalog().status()


@app.get("/api/admin/catalog/status", dependencies=[Depends(require_admin)])
def admin_catalog_status() -> dict[str, object]:
    settings = get_settings()
    status = _catalog().status()
    status["backups"] = list_catalog_backups(settings.storage_dir / "catalog-backups")
    status["admin_token_required"] = bool(settings.admin_token)
    status["active_job"] = running_catalog_job()
    get_runtime_billing_rule_set()
    status["billing_rules"] = rule_store_status()
    return status


@app.post("/api/admin/catalog/validate", dependencies=[Depends(require_admin)])
async def validate_catalog_upload(file: UploadFile = File(...)) -> dict[str, object]:
    settings = get_settings()
    uploaded_path = await save_upload(file, settings.storage_dir / "admin-catalog-uploads")
    try:
        return validate_catalog_database(uploaded_path)
    except CatalogValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/admin/catalog/upload", dependencies=[Depends(require_admin)])
async def upload_catalog_database(file: UploadFile = File(...)) -> dict[str, object]:
    active_job = running_catalog_job()
    if active_job:
        raise HTTPException(status_code=409, detail=f"Catalog job {active_job['id']} is still running.")

    settings = get_settings()
    uploaded_path = await save_upload(file, settings.storage_dir / "admin-catalog-uploads")
    try:
        install_result = install_catalog_database(
            uploaded_path=uploaded_path,
            target_path=settings.catalog_db_path,
            backup_dir=settings.storage_dir / "catalog-backups",
        )
    except CatalogValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "import": install_result,
        "status": admin_catalog_status(),
    }


@app.post("/api/admin/catalog/regional/import", dependencies=[Depends(require_admin)])
async def import_regional_catalog(
    file: UploadFile = File(...),
    quarter: str = Form(...),
    region: str = Form("Hessen"),
    source_system: str = Form("KV_HESSEN_GOP"),
    catalog_id: str = Form(""),
    replace: bool = Form(True),
) -> dict[str, object]:
    active_job = running_catalog_job()
    if active_job:
        raise HTTPException(status_code=409, detail=f"Catalog job {active_job['id']} is still running.")

    if file.content_type not in {"application/pdf", "application/octet-stream"} and not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Für regionale Kataloge werden nur PDF-Dateien unterstützt.")

    settings = get_settings()
    uploaded_path = await save_upload(file, settings.storage_dir / "admin-regional-uploads")
    try:
        import_result = import_regional_catalog_pdf(
            pdf_path=uploaded_path,
            target_path=settings.catalog_db_path,
            backup_dir=settings.storage_dir / "catalog-backups",
            work_dir=settings.storage_dir / "catalog-work",
            catalog_id=catalog_id.strip() or None,
            source_system=source_system.strip() or "KV_HESSEN_GOP",
            region=region.strip() or "Hessen",
            quarter=quarter.strip(),
            replace=replace,
        )
    except (CatalogImportError, CatalogValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "import": import_result,
        "status": admin_catalog_status(),
    }


@app.post("/api/admin/catalog/ebm/scrape", status_code=202, dependencies=[Depends(require_admin)])
def scrape_ebm_catalog(
    quarter: str = Form(...),
    replace_quarter: bool = Form(True),
    delay: float = Form(0.02),
    timeout: int = Form(30),
) -> dict[str, object]:
    settings = get_settings()
    requested_quarter = quarter.strip()
    params = {
        "quarter": requested_quarter,
        "replace_quarter": replace_quarter,
        "delay": delay,
        "timeout": timeout,
    }

    def run_scrape() -> dict[str, object]:
        import_result = scrape_ebm_quarter_into_catalog(
            target_path=settings.catalog_db_path,
            backup_dir=settings.storage_dir / "catalog-backups",
            work_dir=settings.storage_dir / "catalog-work",
            quarter=requested_quarter,
            replace_quarter=replace_quarter,
            delay=delay,
            timeout=timeout,
            commit_every=100,
            progress_every=250,
        )
        return {
            "import": import_result,
            "status": admin_catalog_status(),
        }

    try:
        job = start_catalog_job(
            kind="ebm_scrape",
            params=params,
            message=f"KBV-EBM {requested_quarter} wird im Hintergrund importiert.",
            target=run_scrape,
        )
    except JobAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (CatalogImportError, CatalogValidationError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "job": job,
        "status": admin_catalog_status(),
    }


@app.get("/api/admin/catalog/jobs/{job_id}", dependencies=[Depends(require_admin)])
def admin_catalog_job(job_id: str) -> dict[str, object]:
    try:
        job = get_job(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Katalogjob nicht gefunden.") from exc
    return {
        "job": job,
        "status": admin_catalog_status(),
    }


@app.post("/api/admin/rules/compile", status_code=202, dependencies=[Depends(require_admin)])
def compile_billing_rules(
    quarter: str = Form(...),
    region: str = Form("Hessen"),
) -> dict[str, object]:
    settings = get_settings()
    requested_quarter = quarter.strip().upper()
    requested_region = region.strip() or "Hessen"
    if not re.fullmatch(r"\d{4}/Q[1-4]", requested_quarter):
        raise HTTPException(status_code=400, detail="Das Quartal muss im Format JJJJ/Q1 bis JJJJ/Q4 angegeben werden.")
    params = {"quarter": requested_quarter, "region": requested_region}

    def run_compilation() -> dict[str, object]:
        return compile_and_migrate_catalog_rules(
            catalog_db_path=settings.catalog_db_path,
            quarter=requested_quarter,
            region=requested_region,
        )

    try:
        job = start_catalog_job(
            kind="rule_compile",
            params=params,
            message=f"EBM-Regelwerk {requested_quarter} wird kompiliert und nach Supabase migriert.",
            target=run_compilation,
        )
    except JobAlreadyRunningError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"job": job, "status": admin_catalog_status()}


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
    return rule_overview_payload()


def _analyze_uploaded_pdf(uploaded_path, source_filename: str, settings: Settings) -> AnalysisResult:
    analysis_dir = settings.storage_dir / "analyses"

    pages, warnings = extract_pages(uploaded_path, settings)
    segments = segment_pages(pages)
    evidence, review_candidates, excluded, case_context = extract_evidence(pages, segments)

    catalog = _catalog()
    default_quarter = case_context.get("quarter") or "2025/Q4"
    region = str(case_context.get("region") or "Hessen")
    billing_events = build_billing_events(evidence, str(default_quarter), region)
    episode_selection = episode_selection_payload(billing_events)
    for episode in episode_selection["episodes"]:
        if episode["primary"]:
            continue
        review_candidates.append(
            ReviewCandidate(
                evidence="Separater Behandlungsabschnitt",
                evidence_pages=episode["evidence_pages"],
                reason=(
                    f"Der Zeitraum {episode['start_date'] or '?'} bis {episode['end_date'] or '?'} liegt mehr als "
                    f"{episode_selection['episode_gap_days']} Tage vom primären Abrechnungsabschnitt entfernt und wurde "
                    "nicht in diesen Rechnungsentwurf übernommen."
                ),
            )
        )
    analysis_warnings = list(warnings)
    billing_derivation: dict[str, object]
    if settings.enable_semantic_billing:
        try:
            semantic_result = generate_semantic_billing_items(
                evidence,
                catalog,
                default_quarter=default_quarter,
                settings=settings,
                region=region,
            )
            items = semantic_result.items
            summary = semantic_result.summary
            review_candidates.extend(semantic_result.review_candidates)
            excluded.extend(semantic_result.excluded_evidence)
            billing_derivation = semantic_result.context
        except SemanticBillingError as exc:
            analysis_warnings.append(f"Semantische Abrechnung fehlgeschlagen, deterministische Regeln werden verwendet: {exc}")
            items, summary = generate_billing_items(evidence, catalog, default_quarter=default_quarter, region=region)
            billing_derivation = {
                "mode": "deterministic_rules",
                "fallback_reason": str(exc),
            }
        except Exception as exc:  # pragma: no cover - safety net for external LLM integration.
            analysis_warnings.append(f"Semantische Abrechnung ist abgestürzt, deterministische Regeln werden verwendet: {exc}")
            items, summary = generate_billing_items(evidence, catalog, default_quarter=default_quarter, region=region)
            billing_derivation = {
                "mode": "deterministic_rules",
                "fallback_reason": f"Unerwarteter Fehler der semantischen Abrechnung: {exc}",
            }
    else:
        items, summary = generate_billing_items(evidence, catalog, default_quarter=default_quarter, region=region)
        billing_derivation = {"mode": "deterministic_rules", "fallback_reason": "Semantische Abrechnung ist deaktiviert."}

    item_quarters = sorted({item.quarter for item in items}) or [str(default_quarter)]
    regional_catalog_checks = [
        catalog.regional_catalog_check(
            [item.gop_base for item in items if item.quarter == quarter],
            quarter=quarter,
            region=region,
        )
        for quarter in item_quarters
    ]

    catalog_context = catalog.status()
    catalog_context["analysis_warnings"] = analysis_warnings
    catalog_context["case_context"] = case_context
    catalog_context["episode_selection"] = episode_selection
    catalog_context["billing_derivation"] = billing_derivation
    catalog_context["regional_catalog_checks"] = regional_catalog_checks

    result = AnalysisResult(
        analysis_id=uuid4().hex,
        status="draft_needs_human_review",
        source_filename=source_filename,
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
    try:
        save_invoice(result)
    except Exception as exc:
        if settings.supabase_url and settings.supabase_key:
            raise RuntimeError(f"Rechnungsentwurf konnte nicht in Supabase gespeichert werden: {exc}") from exc
    return result


def _analysis_job_result(result: AnalysisResult) -> dict[str, object]:
    return {
        "analysis_id": result.analysis_id,
        "source_filename": result.source_filename,
        "summary": result.summary.model_dump(),
    }


def _validate_pdf_upload(file: UploadFile) -> None:
    if file.content_type not in {"application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Nur PDF-Uploads werden unterstützt.")


@app.post("/api/documents/analyze", response_model=AnalysisResult, dependencies=[Depends(require_admin)])
async def analyze_document(file: UploadFile = File(...)) -> AnalysisResult:
    _validate_pdf_upload(file)

    settings = get_settings()
    upload_dir = settings.storage_dir / "uploads"
    uploaded_path = await save_upload(file, upload_dir)
    try:
        return _analyze_uploaded_pdf(uploaded_path, file.filename or uploaded_path.name, settings)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/api/documents/analyze/jobs", status_code=202, dependencies=[Depends(require_admin)])
async def start_document_analysis(file: UploadFile = File(...)) -> dict[str, object]:
    _validate_pdf_upload(file)

    settings = get_settings()
    upload_dir = settings.storage_dir / "uploads"
    uploaded_path = await save_upload(file, upload_dir)
    source_filename = file.filename or uploaded_path.name

    def run_analysis() -> dict[str, object]:
        result = _analyze_uploaded_pdf(uploaded_path, source_filename, settings)
        return _analysis_job_result(result)

    job = start_analysis_job(
        params={"source_filename": source_filename},
        message="Analyse wurde gestartet.",
        target=run_analysis,
    )
    return {"job": job}


@app.get("/api/documents/analyze/jobs/{job_id}", dependencies=[Depends(require_admin)])
def document_analysis_job(job_id: str) -> dict[str, object]:
    try:
        job = get_analysis_job(job_id)
    except AnalysisJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Analysejob nicht gefunden.") from exc
    return {"job": job}


@app.get("/api/analyses/{analysis_id}", response_model=AnalysisResult, dependencies=[Depends(require_admin)])
def get_analysis(analysis_id: str) -> AnalysisResult:
    settings = get_settings()
    result = load_invoice(analysis_id)
    if not result:
        result = load_analysis(analysis_id, settings.storage_dir / "analyses")
    if not result:
        raise HTTPException(status_code=404, detail="Rechnungsentwurf nicht gefunden.")
    return result


@app.get("/api/invoices", dependencies=[Depends(require_admin)])
def invoices(limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0)) -> dict[str, object]:
    settings = get_settings()
    return list_invoices(limit=limit, offset=offset, analysis_dir=settings.storage_dir / "analyses")


@app.get("/api/invoices/{analysis_id}", response_model=AnalysisResult, dependencies=[Depends(require_admin)])
def invoice(analysis_id: str) -> AnalysisResult:
    return get_analysis(analysis_id)


@app.delete("/api/invoices/{analysis_id}", dependencies=[Depends(require_admin)])
def remove_invoice(analysis_id: str) -> dict[str, object]:
    settings = get_settings()
    try:
        result = delete_invoice(analysis_id, settings.storage_dir / "analyses")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Rechnungsentwurf konnte nicht gelöscht werden: {exc}") from exc
    if not result["deleted"]:
        raise HTTPException(status_code=404, detail="Rechnungsentwurf nicht gefunden.")
    return result
