from __future__ import annotations

from dataclasses import dataclass

from .billing_rules import BillingRuleContext, evaluate_catalog_context_rules, resolve_evidence_rule_gop
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
    BillingRule("radiology.xray_shoulder_2_planes.34231.v1", "radiology.xray_shoulder_2_planes", "34231", "Aufnahmen der Schulter/des Schultergürtels"),
    BillingRule("radiology.xray_spine_hws_2_planes.34221.v1", "radiology.xray_spine_hws_2_planes", "34221", "Aufnahmen von Teilen der Wirbelsäule"),
    BillingRule("radiology.xray_thorax_2_planes.34241.v1", "radiology.xray_thorax_2_planes", "34241", "Röntgen Thorax/Lunge 2 Ebenen"),
    BillingRule("radiology.xray_hand_foot.34232.v1", "radiology.xray_hand_foot", "34232", "Aufnahmen der Hand, des Fußes"),
    BillingRule("radiology.xray_extremities.34233.v1", "radiology.xray_extremities", "34233", "Aufnahmen der Extremitäten"),
    BillingRule("radiology.ct_spine_section.34311.v1", "radiology.ct_spine_section", "34311", "CT Wirbelsäulenabschnitt"),
    BillingRule("radiology.ct_extremities.34350.v1", "radiology.ct_extremities", "34350", "CT-Untersuchung der Extremitäten außer Hand/Fuß"),
    BillingRule("radiology.ct_hand_foot.34351.v1", "radiology.ct_hand_foot", "34351", "CT-Untersuchung der Hand, des Fußes"),
    BillingRule("radiology.ct_contrast.34345.v1", "radiology.ct_contrast", "34345", "CT-Kontrastmittelzuschlag"),
    BillingRule("lab.quick.32113.v1", "lab.quick", "32113", "Quick-Wert, Plasma"),
    BillingRule("lab.creatinine.32066.v1", "lab.creatinine", "32066", "Kreatinin"),
    BillingRule("lab.sodium.32083.v1", "lab.sodium", "32083", "Natrium"),
    BillingRule("lab.potassium.32081.v1", "lab.potassium", "32081", "Kalium"),
    BillingRule("lab.glucose.32025.v1", "lab.glucose", "32025", "Glucose"),
    BillingRule("lab.alt_gpt.32070.v1", "lab.alt_gpt", "32070", "GPT / ALT"),
    BillingRule("lab.blood_count.erythrocytes.32035.v1", "lab.erythrocytes", "32035A", "Erythrozytenzählung"),
    BillingRule("lab.blood_count.leukocytes.32036.v1", "lab.leukocytes", "32036A", "Leukozytenzählung"),
    BillingRule("lab.blood_count.thrombocytes.32037.v1", "lab.thrombocytes", "32037A", "Thrombozytenzählung"),
    BillingRule("lab.blood_count.hemoglobin.32038.v1", "lab.hemoglobin", "32038A", "Hämoglobin"),
    BillingRule("lab.blood_count.hematocrit.32039.v1", "lab.hematocrit", "32039A", "Hämatokrit"),
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
        rule_decision = resolve_evidence_rule_gop(
            rule.evidence_kind,
            rule.gop_original,
            selected.service_date,
            selected.service_time,
            region,
        )
        gop_original = rule_decision.gop or rule.gop_original
        gop_base, gop_suffix = normalize_gop(gop_original)
        if gop_base in used_bases:
            continue
        used_bases.add(gop_base)

        quarter = default_quarter or quarter_from_date(selected.service_date) or "2025/Q4"
        entry = catalog.lookup(gop_original, quarter, region=region)
        validation_notes = list(rule_decision.notes)
        validation_status = "review" if rule_decision.review_required else "valid"
        rule_id = f"{rule.rule_id}+{rule_decision.rule_id}"

        if not entry:
            validation_status = "catalog_missing"
            validation_notes.append(f"GOP {gop_base} wurde im Katalog {quarter} nicht gefunden.")
            title = rule.title_hint
            points = None
            amount = None
            source = "UNKNOWN"
            source_label = None
            catalog_id = None
            catalog_data_stand = None
        else:
            catalog_decision = evaluate_catalog_context_rules(
                BillingRuleContext(
                    gop=gop_original,
                    service_date=selected.service_date,
                    service_time=selected.service_time,
                    region=region,
                    evidence_kind=rule.evidence_kind,
                    evidence_text=selected.text,
                    evidence_metadata=selected.metadata,
                    catalog_rule_texts=entry.rule_texts,
                )
            )
            validation_notes.extend(catalog_decision.notes)
            if catalog_decision.review_required:
                validation_status = "review"
            if catalog_decision.rule_id != "catalog.context.noop.v1":
                rule_id = f"{rule_id}+{catalog_decision.rule_id}"
            title = entry.title
            points = entry.points
            amount = entry.euro
            source = entry.source
            source_label = entry.catalog_label
            catalog_id = entry.catalog_id
            catalog_data_stand = entry.data_stand

        items.append(
            BillingItem(
                line=len(items) + 1,
                gop_original=gop_original,
                gop_base=gop_base,
                gop_suffix=gop_suffix,
                title=title,
                catalog_source=source,
                catalog_source_label=source_label,
                catalog_id=catalog_id,
                catalog_data_stand=catalog_data_stand,
                quarter=quarter,
                service_date=selected.service_date,
                service_time=selected.service_time,
                quantity=1,
                points=points,
                amount_eur=amount,
                rule_id=rule_id,
                confidence="medium" if rule_decision.review_required else rule.confidence,
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
