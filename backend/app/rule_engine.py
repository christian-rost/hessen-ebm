from __future__ import annotations

from dataclasses import dataclass

from .catalog import CatalogRepository, normalize_gop
from .evidence_extraction import quarter_from_date
from .models import BillingItem, Evidence, InvoiceSummary


@dataclass(frozen=True)
class BillingRule:
    rule_id: str
    evidence_kind: str
    gop_original: str
    title_hint: str
    confidence: str = "high"


ACTIVE_RULES: list[BillingRule] = [
    BillingRule("context.kv_notfall_zna.01210.v1", "context.kv_notfall_zna", "01210", "Notfallpauschale I"),
    BillingRule("radiology.ct_head_native.34310.v1", "radiology.ct_head_native", "34310", "CT-Untersuchung des Neurocraniums"),
    BillingRule("radiology.xray_shoulder_2_planes.34231.v1", "radiology.xray_shoulder_2_planes", "34231", "Aufnahmen der Schulter/des Schulterguertels"),
    BillingRule("radiology.xray_spine_hws_2_planes.34221.v1", "radiology.xray_spine_hws_2_planes", "34221", "Aufnahmen von Teilen der Wirbelsaeule"),
    BillingRule("radiology.xray_thorax_2_planes.34241.v1", "radiology.xray_thorax_2_planes", "34241", "Roentgen Thorax/Lunge 2 Ebenen"),
    BillingRule("radiology.ct_spine_section.34311.v1", "radiology.ct_spine_section", "34311", "CT Wirbelsaeulenabschnitt"),
    BillingRule("radiology.ct_contrast.34345.v1", "radiology.ct_contrast", "34345", "CT-Kontrastmittelzuschlag"),
    BillingRule("lab.quick.32113.v1", "lab.quick", "32113", "Quick-Wert, Plasma"),
    BillingRule("lab.creatinine.32066.v1", "lab.creatinine", "32066", "Kreatinin"),
    BillingRule("lab.sodium.32083.v1", "lab.sodium", "32083", "Natrium"),
    BillingRule("lab.potassium.32081.v1", "lab.potassium", "32081", "Kalium"),
    BillingRule("lab.glucose.32025.v1", "lab.glucose", "32025", "Glucose"),
    BillingRule("lab.alt_gpt.32070.v1", "lab.alt_gpt", "32070", "GPT / ALT"),
    BillingRule("lab.blood_count.erythrocytes.32035.v1", "lab.erythrocytes", "32035A", "Erythrozytenzaehlung"),
    BillingRule("lab.blood_count.leukocytes.32036.v1", "lab.leukocytes", "32036A", "Leukozytenzaehlung"),
    BillingRule("lab.blood_count.thrombocytes.32037.v1", "lab.thrombocytes", "32037A", "Thrombozytenzaehlung"),
    BillingRule("lab.blood_count.hemoglobin.32038.v1", "lab.hemoglobin", "32038A", "Haemoglobin"),
    BillingRule("lab.blood_count.hematocrit.32039.v1", "lab.hematocrit", "32039A", "Haematokrit"),
]


def active_rules_payload() -> list[dict[str, str]]:
    return [
        {
            "rule_id": rule.rule_id,
            "evidence_kind": rule.evidence_kind,
            "gop_original": rule.gop_original,
            "title_hint": rule.title_hint,
        }
        for rule in ACTIVE_RULES
    ]


def generate_billing_items(
    evidence: list[Evidence],
    catalog: CatalogRepository,
    default_quarter: str | None,
    region: str = "Hessen",
) -> tuple[list[BillingItem], InvoiceSummary]:
    evidence_by_kind: dict[str, list[Evidence]] = {}
    for item in evidence:
        evidence_by_kind.setdefault(item.kind, []).append(item)

    items: list[BillingItem] = []
    used_bases: set[str] = set()

    for rule in ACTIVE_RULES:
        matches = evidence_by_kind.get(rule.evidence_kind, [])
        if not matches:
            continue

        selected = _select_best_evidence(matches)
        gop_base, gop_suffix = normalize_gop(rule.gop_original)
        if gop_base in used_bases:
            continue
        used_bases.add(gop_base)

        quarter = default_quarter or quarter_from_date(selected.service_date) or "2025/Q4"
        entry = catalog.lookup(rule.gop_original, quarter, region=region)
        validation_notes: list[str] = []
        validation_status = "valid"

        if not entry:
            validation_status = "catalog_missing"
            validation_notes.append(f"GOP {gop_base} wurde im Katalog {quarter} nicht gefunden.")
            title = rule.title_hint
            points = None
            amount = None
            source = "UNKNOWN"
        else:
            title = entry.title
            points = entry.points
            amount = entry.euro
            source = entry.source

        items.append(
            BillingItem(
                line=len(items) + 1,
                gop_original=rule.gop_original,
                gop_base=gop_base,
                gop_suffix=gop_suffix,
                title=title,
                catalog_source=source,
                quarter=quarter,
                service_date=selected.service_date,
                service_time=selected.service_time,
                quantity=1,
                points=points,
                amount_eur=amount,
                rule_id=rule.rule_id,
                confidence=rule.confidence,
                evidence_ids=[ev.evidence_id for ev in matches],
                evidence_pages=sorted({ev.page for ev in matches}),
                validation_status=validation_status,  # type: ignore[arg-type]
                validation_notes=validation_notes,
            )
        )

    summary = InvoiceSummary(
        line_count=len(items),
        points_total=sum((item.points or 0) * item.quantity for item in items),
        amount_total_eur=round(sum((item.amount_eur or 0.0) * item.quantity for item in items), 2),
        human_review_required=True,
    )
    return items, summary


def _select_best_evidence(matches: list[Evidence]) -> Evidence:
    def score(item: Evidence) -> tuple[int, str, float]:
        has_date = 1 if item.service_date else 0
        service_datetime = f"{item.service_date or ''}T{item.service_time or '00:00'}"
        return has_date, service_datetime, item.confidence

    return sorted(matches, key=score, reverse=True)[0]
