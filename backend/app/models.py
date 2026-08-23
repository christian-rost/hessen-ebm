from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class SelectionEntry(BaseModel):
    code: str
    label: str | None = None
    quantity: float | None = None
    state: Literal["checked", "unchecked", "ambiguous"]
    confidence: float
    source: Literal["pdf_vector", "ocr_text", "merged"]
    bbox: tuple[float, float, float, float] | None = None


class PageText(BaseModel):
    page: int
    text: str
    provider: str = "pdfplumber"
    selection_entries: list[SelectionEntry] = Field(default_factory=list)


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
    catalog_id: str | None = None
    catalog_label: str | None = None
    data_stand: str | None = None
    gop: str
    gop_base: str
    title: str
    points: int | None = None
    euro: float | None = None
    region: str | None = None
    page: int | None = None
    description: str | None = None
    rule_texts: list[str] = Field(default_factory=list)
    # Stellung im Katalogbaum, von der Kapitelebene bis zum führenden Abschnitt.
    # Der Abschnitt sagt, für welchen Versorgungszusammenhang eine GOP gilt.
    section_path: list[str] = Field(default_factory=list)


class BillingItem(BaseModel):
    line: int
    gop_original: str
    gop_base: str
    gop_suffix: str | None = None
    title: str
    catalog_source: str
    catalog_source_label: str | None = None
    catalog_id: str | None = None
    catalog_data_stand: str | None = None
    quarter: str
    service_date: str | None = None
    service_time: str | None = None
    service_event_id: str | None = None
    service_session_id: str | None = None
    treatment_episode_id: str | None = None
    temporal_sequence: int | None = None
    temporal_role: str = "service_event"
    temporal_reason: str | None = None
    temporal_rule_id: str | None = None
    covered_service_content: list[str] = Field(default_factory=list)
    # Die Gegenseite: was die Kataloglegende verlangt und die Doku nicht hergibt.
    # Blockiert die Position nicht zwingend, gehoert aber vor die Augen des Arztes -
    # sonst steht eine Position auf der Rechnung, deren Beleg eine Luecke hat.
    missing_service_content: list[str] = Field(default_factory=list)
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
    # Seiten allein reichen fuer die Nachverfolgung nicht: mehrere Belege teilen sich
    # eine Seite, und ohne Id laesst sich nicht feststellen, ob ein Beleg irgendwo
    # geblieben ist. Optional, damit aeltere gespeicherte Entwuerfe weiter laden.
    evidence_ids: list[str] = Field(default_factory=list)


class ExcludedEvidence(BaseModel):
    evidence: str
    evidence_pages: list[int]
    reason: str
    not_billed_gop: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class InvoiceSummary(BaseModel):
    line_count: int
    points_total: int
    amount_total_eur: float
    currency: str = "EUR"
    human_review_required: bool = True


class InvoiceTimelineEvent(BaseModel):
    event_id: str
    sequence: int
    event_type: str
    label: str
    service_date: str | None = None
    service_time: str | None = None
    temporal_role: str = "service_event"
    reason: str | None = None
    gops: list[str] = Field(default_factory=list)
    billing_item_lines: list[int] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_pages: list[int] = Field(default_factory=list)


class DocumentationHint(BaseModel):
    """Leistung, die erbracht sein duerfte, deren Dokumentation aber nicht traegt.

    Konzept 3.4 trennt "medizinisch erbracht" von "abrechnungsfaehig". Wer beides
    in einer Entscheidung zusammenzieht, verliert die interessantesten Faelle
    lautlos: Die Leistung steht in der Akte, ein obligater Inhalt fehlt in der
    Doku - und der Vorschlag verschwindet, ohne dass jemand erfaehrt, woran es lag.
    Hier wird er stattdessen benannt, mitsamt dem, was zur Abrechnung fehlt.
    """

    gop: str
    gop_base: str
    title: str | None = None
    quarter: str | None = None
    service_date: str | None = None
    points: int | None = None
    euro: float | None = None
    catalog_source: str | None = None
    catalog_id: str | None = None
    catalog_data_stand: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    evidence_pages: list[int] = Field(default_factory=list)
    covered_service_content: list[str] = Field(default_factory=list)
    # Das eigentliche Ergebnis: was die Akte fuer diese Position noch hergeben muesste.
    missing_service_content: list[str] = Field(default_factory=list)
    confidence: str = "medium"
    reason: str | None = None
    origin: str = "semantic_llm"


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
    timeline_events: list[InvoiceTimelineEvent] = Field(default_factory=list)
    items: list[BillingItem]
    review_candidates: list[ReviewCandidate]
    excluded_evidence: list[ExcludedEvidence]
    documentation_hints: list[DocumentationHint] = Field(default_factory=list)
    summary: InvoiceSummary
