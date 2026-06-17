from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PageText(BaseModel):
    page: int
    text: str
    provider: str = "pdfplumber"


class DocumentSegment(BaseModel):
    segment_id: str
    segment_type: str
    title: str
    start_page: int
    end_page: int
    relevant_for_billing: bool
    confidence: float
    reasons: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    evidence_id: str
    kind: str
    label: str
    page: int
    service_date: str | None = None
    service_time: str | None = None
    value: str | None = None
    unit: str | None = None
    text: str
    confidence: float = 0.8
    metadata: dict[str, Any] = Field(default_factory=dict)


class CatalogEntry(BaseModel):
    source: Literal["EBM_KBV", "KV_HESSEN_GOP"]
    quarter: str
    gop: str
    gop_base: str
    title: str
    points: int | None = None
    euro: float | None = None
    region: str | None = None
    page: int | None = None


class BillingItem(BaseModel):
    line: int
    gop_original: str
    gop_base: str
    gop_suffix: str | None = None
    title: str
    catalog_source: str
    quarter: str
    service_date: str | None = None
    service_time: str | None = None
    quantity: int = 1
    points: int | None = None
    amount_eur: float | None = None
    rule_id: str
    confidence: str
    evidence_ids: list[str]
    evidence_pages: list[int]
    validation_status: Literal["valid", "catalog_missing", "review"] = "valid"
    validation_notes: list[str] = Field(default_factory=list)
    derivation_source: Literal["semantic_llm", "deterministic_rules"] = "deterministic_rules"
    semantic_reason: str | None = None
    semantic_catalog_candidates: list[str] = Field(default_factory=list)


class ReviewCandidate(BaseModel):
    evidence: str
    evidence_pages: list[int]
    reason: str
    possible_gops: list[str] = Field(default_factory=list)


class ExcludedEvidence(BaseModel):
    evidence: str
    evidence_pages: list[int]
    reason: str
    not_billed_gop: str | None = None


class InvoiceSummary(BaseModel):
    line_count: int
    points_total: int
    amount_total_eur: float
    currency: str = "EUR"
    human_review_required: bool = True


class AnalysisResult(BaseModel):
    analysis_id: str
    export_profile: str = "EBM_KVDT_ADT_LIKE_V1_DRAFT"
    status: str
    source_filename: str
    source_sha256: str
    catalog_context: dict[str, Any]
    pages: list[PageText]
    segments: list[DocumentSegment]
    evidence: list[Evidence]
    items: list[BillingItem]
    review_candidates: list[ReviewCandidate]
    excluded_evidence: list[ExcludedEvidence]
    summary: InvoiceSummary
