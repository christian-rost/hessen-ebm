from pathlib import Path

from app.billing_rule_definitions import parse_billing_rule_set
from app.catalog import CatalogRepository
from app.models import BillingItem, CatalogEntry, Evidence
from app.config import Settings
from app.rule_engine import reconcile_derived_item_anchors
from app.semantic_billing import _search_terms, generate_semantic_billing_items


class FakeCatalog(CatalogRepository):
    # Von derive_items gefuellt: Suchbegriff -> GOPs, die das Retrieval liefert.
    term_index: dict[str, tuple[str, ...]] = {}

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
            "32247": ("Bestimmung der Blutgase und des Säure-Basen-Status", None, 9.50),
            "34310": ("CT-Untersuchung des Neurocraniums", 534, 66.18),
            "34330": ("CT-Untersuchung des Thorax", 586, 74.65),
            "34345": ("Zuschlag CT mit Kontrastmittel", 216, 27.52),
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
        if base not in values:
            return None
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

    def lookup_ebm(self, gop: str, quarter: str):
        return self.lookup(gop, quarter)

    def lookup_hessen(self, gop: str, quarter: str, region: str = "Hessen"):
        return None

    def search(self, query: str, quarter: str, limit: int = 25):
        gops: list[str] = []
        for gop in self.term_index.get(query, ()):
            if gop not in gops:
                gops.append(gop)
        entries = [entry for gop in gops if (entry := self.lookup(gop, quarter))]
        return entries[:limit]


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


# Welche Evidenzformulierung welche Katalogeintraege findet. Frueher stand diese
# Zuordnung als Allowlist im Regelwerk; sie ist jetzt Testfixture, weil die
# Produktivableitung sie aus dem Quartalskatalog holt.
SEARCH_HINTS: dict[str, tuple[str, ...]] = {
    "context.kv_notfall_zna": ("01210",),
    "clinical.diagnostics.ctg": ("01786",),
    "clinical.diagnostics.abdominal_sonography": ("33042",),
    "lab.blood_gas_analysis": ("32247",),
    "radiology.ct_head_native": ("34310",),
    "radiology.ct_thorax": ("34330",),
    "radiology.ct_contrast": ("34345",),
    "radiology.ct_extremities": ("34350",),
    "radiology.ct_hand_foot": ("34351",),
    "radiology.xray_shoulder_2_planes": ("34231",),
    "radiology.xray_spine_hws_2_planes": ("34221",),
    "radiology.xray_hand_foot": ("34232",),
    "radiology.xray_extremities": ("34233",),
    "lab.quick": ("32113",),
    "lab.creatinine": ("32066",),
    "lab.sodium": ("32083",),
    "lab.potassium": ("32081",),
    "lab.glucose": ("32025",),
    "lab.alt_gpt": ("32070",),
    "lab.erythrocytes": ("32035A",),
    "lab.leukocytes": ("32036A",),
    "lab.thrombocytes": ("32037A",),
    "lab.hemoglobin": ("32038A",),
    "lab.hematocrit": ("32039A",),
}


def _settings() -> Settings:
    return Settings(
        app_env="test",
        log_level="info",
        catalog_db_path=Path("/not-used.sqlite"),
        storage_dir=Path("/tmp"),
        admin_token=None,
        enable_mistral_ocr=False,
        enable_semantic_billing=True,
        mistral_api_key=None,
        mistral_ocr_model="mistral-ocr-latest",
        mistral_llm_model="mistral-large-latest",
    )


def derive_items(evidence, catalog, default_quarter, region="Hessen"):
    """Treibt die produktive Ableitung mit einem LLM-Stub.

    Der Stub schlaegt je Evidenz die Basis-GOP vor, die das Retrieval gefunden
    hat. Zeit-, Sequenz-, Zuschlags- und Katalogregeln laufen danach unveraendert
    im Produktivcode; genau die pruefen diese Tests.
    """

    # Retrieval modellieren: jeder Suchbegriff, den die Evidenz erzeugt, findet
    # die GOPs ihrer Evidenzart. Im Produktivbetrieb macht das die FTS-Suche.
    term_index: dict[str, list[str]] = {}
    for item in evidence:
        for term in _search_terms(item):
            term_index.setdefault(term, []).extend(
                gop for gop in SEARCH_HINTS.get(item.kind, ()) if gop not in term_index.get(term, [])
            )
    catalog.term_index = {term: tuple(gops) for term, gops in term_index.items()}

    def fake_llm(_messages, _settings):
        # Evidenz derselben Art am selben Leistungstag ist ein Leistungsereignis
        # und ergibt genau einen Vorschlag, so wie es ein sorgfaeltiges LLM taete.
        grouped: dict[tuple[str, str | None], list] = {}
        for item in evidence:
            if item.kind in SEARCH_HINTS:
                grouped.setdefault((item.kind, item.service_date), []).append(item)
        items = []
        for (kind, service_date), members in grouped.items():
            items.append(
                {
                    "gop": SEARCH_HINTS[kind][0],
                    "quantity": 1,
                    "evidence_ids": [member.evidence_id for member in members],
                    "service_date": service_date,
                    "service_time": members[0].service_time,
                    "confidence": "high",
                    "reason": f"Evidenz {kind} dokumentiert.",
                }
            )
        return {"items": items, "review_candidates": [], "excluded_evidence": []}

    result = generate_semantic_billing_items(
        evidence,
        catalog,
        default_quarter=default_quarter,
        settings=_settings(),
        region=region,
        llm_client=fake_llm,
    )
    return result.items, result.summary


def test_kv_notfall_zna_daytime_uses_01210():
    evidence = [ev("context.kv_notfall_zna", service_date="2026-04-24", service_time="12:20")]

    items, summary = derive_items(evidence, FakeCatalog(), default_quarter="2026/Q2")

    assert [item.gop_original for item in items] == ["01210"]
    assert summary.points_total == 120
    assert summary.amount_total_eur == 14.87


def test_notfall_contact_crossing_midnight_is_one_initial_pauschale():
    evidence = [
        Evidence(
            evidence_id="ev-notfall-before-midnight",
            kind="context.kv_notfall_zna",
            label="Notfallaufnahme",
            page=1,
            service_date="2026-01-29",
            service_time="23:40",
            text="Vorstellung in der Notfallambulanz.",
        ),
        Evidence(
            evidence_id="ev-notfall-after-midnight",
            kind="context.kv_notfall_zna",
            label="Notfallbehandlung",
            page=2,
            service_date="2026-01-30",
            service_time="00:39",
            text="Fortsetzung derselben Notfallbehandlung.",
        ),
    ]

    items, summary = derive_items(evidence, FakeCatalog(), default_quarter="2026/Q1")

    assert [item.gop_original for item in items] == ["01212"]
    assert items[0].service_date == "2026-01-29"
    assert items[0].service_time == "23:40"
    assert items[0].temporal_role == "initial_contact"
    assert summary.points_total == 195


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

    items, summary = derive_items(evidence, FakeCatalog(), default_quarter="2026/Q1")

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

    items, summary = derive_items(evidence, FakeCatalog(), default_quarter="2026/Q1")

    # Die Positionsreihenfolge folgt jetzt der Evidenz im Dokument, nicht mehr der
    # Reihenfolge einer Regelliste.
    assert [item.gop_original for item in items] == ["34233", "34232", "34351"]
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
            kind="clinical.diagnostics.abdominal_sonography",
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

    items, summary = derive_items(evidence, FakeCatalog(), default_quarter="2026/Q1")

    assert [item.gop_original for item in items] == ["01212", "01786", "33042", "01216", "01786"]
    assert [item.temporal_sequence for item in items] == [1, 2, 3, 4, 5]
    assert items[0].temporal_role == "initial_contact"
    assert items[3].temporal_role == "follow_up_contact"
    assert items[1].evidence_pages == [11, 13]
    assert items[1].service_event_id != items[4].service_event_id
    assert summary.points_total == 752
    assert summary.amount_total_eur == 95.12


def test_derived_positions_follow_configured_base_event_without_gop_specific_code():
    rule_set = parse_billing_rule_set(
        {
            "schema_version": 1,
            "rule_set_id": "generic-derived-anchor-test",
            "version": "1",
            "evidence_rules": [],
            "temporal_rules": [],
            "event_sequence_rules": [],
            "derived_rules": [
                {
                    "rule_id": "derived.configured.surcharge.v1",
                    "gop": "22222",
                    "title_hint": "Konfigurierter Zuschlag",
                    "evidence_kind": "configured.support",
                    "insert_after": "11111",
                    "requirements": [{"gop_present": "11111"}],
                    "regions": ["*"],
                }
            ],
        }
    )
    base = BillingItem(
        line=1,
        gop_original="11111",
        gop_base="11111",
        title="Konfigurierte Basisleistung",
        catalog_source="TEST",
        quarter="2026/Q1",
        service_date="2026-02-02",
        service_time="23:49",
        service_event_id="event-base-contact",
        service_session_id="session-contact",
        treatment_episode_id="episode-1",
        rule_id="configured.base.v1",
        confidence="high",
        evidence_ids=["ev-base"],
        evidence_pages=[6],
    )
    wrongly_anchored = BillingItem(
        line=2,
        gop_original="22222",
        gop_base="22222",
        title="Konfigurierter Zuschlag",
        catalog_source="TEST",
        quarter="2026/Q1",
        service_date="2026-02-02",
        service_time="21:23",
        service_event_id="event-supporting-evidence",
        service_session_id="session-admission",
        treatment_episode_id="episode-1",
        rule_id="semantic_llm.22222.v1",
        confidence="high",
        evidence_ids=["ev-support"],
        evidence_pages=[5],
        derivation_source="semantic_llm",
    )
    correctly_anchored = wrongly_anchored.model_copy(
        update={
            "line": 3,
            "service_time": "23:49",
            "service_event_id": "event-base-contact",
            "service_session_id": "session-contact",
            "rule_id": "derived.configured.surcharge.v1",
            "derivation_source": "deterministic_rules",
        }
    )
    items = [wrongly_anchored, base, correctly_anchored]

    reconcile_derived_item_anchors(items, "2026/Q1", "Hessen", rule_set)

    assert [item.gop_original for item in items] == ["11111", "22222"]
    assert items[1].service_time == "23:49"
    assert items[1].service_event_id == "event-base-contact"
    assert items[1].service_session_id == "session-contact"
