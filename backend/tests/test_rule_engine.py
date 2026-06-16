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
            "34310": ("CT-Untersuchung des Neurocraniums", 534, 66.18),
            "34231": ("Aufnahmen der Schulter", 137, 16.98),
            "34221": ("Aufnahmen von Teilen der Wirbelsaeule", 140, 17.35),
            "32113": ("Quick-Wert, Plasma", None, 0.58),
            "32066": ("Kreatinin", None, 0.25),
            "32083": ("Natrium", None, 0.25),
            "32081": ("Kalium", None, 0.25),
            "32025": ("Glucose", None, 1.60),
            "32070": ("GPT", None, 0.25),
            "32035": ("Erythrozytenzaehlung", None, 0.25),
            "32036": ("Leukozytenzaehlung", None, 0.25),
            "32037": ("Thrombozytenzaehlung", None, 0.25),
            "32038": ("Haemoglobin", None, 0.25),
            "32039": ("Haematokrit", None, 0.25),
        }
        base = gop[:5]
        title, points, euro = values[base]
        return CatalogEntry(source="EBM_KBV", quarter=quarter, gop=base, gop_base=base, title=title, points=points, euro=euro)


def ev(kind: str, page: int = 1) -> Evidence:
    return Evidence(evidence_id=f"ev-{kind}", kind=kind, label=kind, page=page, service_date="2025-10-04", service_time="00:01", text=kind)


def test_case_25124444_rule_total():
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
    assert summary.points_total == 931
    assert summary.amount_total_eur == 119.81
    assert [item.gop_original for item in items[:4]] == ["01210", "34310", "34231", "34221"]

