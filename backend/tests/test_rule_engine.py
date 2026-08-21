from pathlib import Path

from app.catalog import CatalogRepository
from app.models import CatalogEntry, Evidence
from app.rule_engine import generate_billing_items


class FakeCatalog(CatalogRepository):
    def __init__(self):
        super().__init__(Path("/not-used.sqlite"))

    def lookup(self, gop: str, quarter: str, region: str = "Hessen"):
        values = {
            "01210": ("Notfallpauschale I", 120, 14.87),
            "01212": ("Notfallpauschale II", 195, 24.16),
            "01214": ("Notfallkonsultationspauschale I", 100, 12.74),
            "01216": ("Notfallkonsultationspauschale II", 140, 17.84),
            "01218": ("Notfallkonsultationspauschale III", 170, 21.66),
            "01226": ("Zuschlag Notfallpauschale zur GOP 01212", 90, 11.15),
            "01786": ("Kardiotokografische Untersuchung", 137, 17.45),
            "33042": ("Sonografie des Abdomens und Retroperitoneums", 143, 18.22),
            "34310": ("CT-Untersuchung des Neurocraniums", 534, 66.18),
            "34231": ("Aufnahmen der Schulter", 137, 16.98),
            "34221": ("Aufnahmen von Teilen der Wirbelsäule", 140, 17.35),
            "34232": ("Aufnahmen der Hand, des Fußes", 99, 12.61),
            "34233": ("Aufnahmen der Extremitäten", 99, 12.61),
            "34350": ("CT-Untersuchung der Extremitäten außer Hand/Fuß", 500, 63.70),
            "34351": ("CT-Untersuchung der Hand, des Fußes", 500, 63.70),
            "32113": ("Quick-Wert, Plasma", None, 0.58),
            "32066": ("Kreatinin", None, 0.25),
            "32083": ("Natrium", None, 0.25),
            "32081": ("Kalium", None, 0.25),
            "32025": ("Glucose", None, 1.60),
            "32070": ("GPT", None, 0.25),
            "32035": ("Erythrozytenzählung", None, 0.25),
            "32036": ("Leukozytenzählung", None, 0.25),
            "32037": ("Thrombozytenzählung", None, 0.25),
            "32038": ("Hämoglobin", None, 0.25),
            "32039": ("Hämatokrit", None, 0.25),
        }
        base = gop[:5]
        title, points, euro = values[base]
        return CatalogEntry(
            source="EBM_KBV",
            quarter=quarter,
            catalog_id=f"ebm_kbv_{quarter.lower().replace('/', '_')}",
            catalog_label=f"KBV EBM {quarter}",
            gop=base,
            gop_base=base,
            title=title,
            points=points,
            euro=euro,
        )


def ev(kind: str, page: int = 1, service_date: str = "2025-10-04", service_time: str = "00:01") -> Evidence:
    return Evidence(
        evidence_id=f"ev-{kind}",
        kind=kind,
        label=kind,
        page=page,
        service_date=service_date,
        service_time=service_time,
        text=kind,
    )


def test_case_FALL-B_rule_total():
    evidence = [
        ev("context.kv_notfall_zna"),
        ev("radiology.ct_head_native"),
        ev("radiology.xray_shoulder_2_planes"),
        ev("radiology.xray_spine_hws_2_planes"),
        ev("lab.quick"),
        ev("lab.creatinine"),
        ev("lab.sodium"),
        ev("lab.potassium"),
        ev("lab.glucose"),
        ev("lab.alt_gpt"),
        ev("lab.erythrocytes"),
        ev("lab.leukocytes"),
        ev("lab.thrombocytes"),
        ev("lab.hemoglobin"),
        ev("lab.hematocrit"),
    ]

    items, summary = generate_billing_items(evidence, FakeCatalog(), default_quarter="2025/Q4")

    assert len(items) == 15
    assert summary.points_total == 1006
    assert summary.amount_total_eur == 129.1
    assert [item.gop_original for item in items[:4]] == ["01212", "34310", "34231", "34221"]
    assert items[0].catalog_source_label == "KBV EBM 2025/Q4"
    assert items[0].rule_id.endswith("time.notfall.initial.01212.v1")


def test_kv_notfall_zna_daytime_uses_01210():
    evidence = [ev("context.kv_notfall_zna", service_date="2026-04-24", service_time="12:20")]

    items, summary = generate_billing_items(evidence, FakeCatalog(), default_quarter="2026/Q2")

    assert [item.gop_original for item in items] == ["01210"]
    assert summary.points_total == 120
    assert summary.amount_total_eur == 14.87


def test_kv_notfall_zna_night_with_dementia_derives_01226_generically():
    evidence = [
        ev("context.kv_notfall_zna", service_date="2026-02-25", service_time="01:13"),
        Evidence(
            evidence_id="ev-diagnosis-f03",
            kind="diagnosis.icd10",
            label="ICD-10 F03",
            page=28,
            service_date="2026-02-25",
            service_time="01:11",
            value="F03",
            text="Aufnahmediagnose Nicht näher bezeichnete Demenz (H) Nicht näher bezeichnete Demenz (F03)",
            metadata={"icd10": "F03"},
        ),
        Evidence(
            evidence_id="ev-cognitive",
            kind="clinical.domain.neurology",
            label="Neurologischer Notfallbericht",
            page=11,
            service_date="2026-02-25",
            service_time="01:13",
            text="Patient 81a, schwere Demenz, zu allen Qualitäten desorientiert, Weglauftendenz.",
        ),
    ]

    items, summary = generate_billing_items(evidence, FakeCatalog(), default_quarter="2026/Q1")

    assert [item.gop_original for item in items] == ["01212", "01226"]
    assert items[1].rule_id.startswith("derived.notfall.01226.v1")
    assert "ev-diagnosis-f03" in items[1].evidence_ids
    assert summary.points_total == 285
    assert summary.amount_total_eur == 35.31


def test_radiology_extremity_hand_and_wrist_ct_rules():
    evidence = [
        ev("radiology.xray_extremities"),
        ev("radiology.xray_hand_foot"),
        ev("radiology.ct_hand_foot"),
    ]

    items, summary = generate_billing_items(evidence, FakeCatalog(), default_quarter="2026/Q1")

    assert [item.gop_original for item in items] == ["34232", "34233", "34351"]
    assert summary.points_total == 698
    assert summary.amount_total_eur == 88.92


def test_temporal_invoice_sequence_preserves_repeated_services_on_different_days():
    evidence = [
        Evidence(
            evidence_id="ev-emergency-jan-1",
            kind="context.kv_notfall_zna",
            label="Notfallambulanz",
            page=8,
            service_date="2026-01-01",
            service_time="13:15",
            text="Vorstellung in der Notfallambulanz am Feiertag.",
        ),
        Evidence(
            evidence_id="ev-ctg-start-jan-1",
            kind="clinical.diagnostics.ctg",
            label="CTG",
            page=11,
            service_date="2026-01-01",
            service_time="13:05",
            text="CTG gestartet, Dauer 21 Minuten.",
        ),
        Evidence(
            evidence_id="ev-ctg-end-jan-1",
            kind="clinical.diagnostics.ctg",
            label="CTG",
            page=13,
            service_date="2026-01-01",
            service_time="13:26",
            text="CTG beendet, FIGO normal.",
        ),
        Evidence(
            evidence_id="ev-renal-sono-jan-1",
            kind="clinical.diagnostics.maternal_renal_sonography",
            label="Sonografie der mütterlichen Nieren",
            page=5,
            service_date="2026-01-01",
            service_time=None,
            text="Rechte Niere mit Hydronephrose Grad II, linke Niere unauffällig.",
        ),
        Evidence(
            evidence_id="ev-emergency-jan-3",
            kind="context.kv_notfall_zna",
            label="Notfallambulanz",
            page=17,
            service_date="2026-01-03",
            service_time="13:19",
            text="Weitere Vorstellung in der Notfallambulanz.",
        ),
        Evidence(
            evidence_id="ev-ctg-jan-3",
            kind="clinical.diagnostics.ctg",
            label="CTG",
            page=19,
            service_date="2026-01-03",
            service_time="13:19",
            text="Erneutes CTG, Dauer 21 Minuten.",
        ),
    ]

    items, summary = generate_billing_items(evidence, FakeCatalog(), default_quarter="2026/Q1")

    assert [item.gop_original for item in items] == ["01212", "01786", "33042", "01216", "01786"]
    assert [item.temporal_sequence for item in items] == [1, 2, 3, 4, 5]
    assert items[0].temporal_role == "initial_contact"
    assert items[3].temporal_role == "follow_up_contact"
    assert items[1].evidence_pages == [11, 13]
    assert items[1].service_event_id != items[4].service_event_id
    assert summary.points_total == 752
    assert summary.amount_total_eur == 95.12
