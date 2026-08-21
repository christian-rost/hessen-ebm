from pathlib import Path
from typing import Optional

from app.catalog import CatalogRepository, normalize_gop
from app.config import Settings
from app.models import CatalogEntry, Evidence
from app.semantic_billing import generate_semantic_billing_items


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
            "01436": ("Konsultationspauschale", 18, 2.29),
            "06212": ("Grundpauschale ab 60. Lebensjahr", 136, 17.33),
            "06310": ("Fortlaufende Tonometrie", 101, 12.87),
            "06333": ("Binokulare Untersuchung des Augenhintergrundes", 53, 6.75),
            "33000": ("Sonografie des Auges", 95, 12.10),
            "32066": ("Kreatinin", None, 0.25),
        }
        base, _ = normalize_gop(gop)
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
        return []


class RuleTextCatalog(FakeCatalog):
    def lookup(self, gop: str, quarter: str, region: str = "Hessen"):
        entry = super().lookup(gop, quarter, region)
        if entry and entry.gop_base == "32066":
            entry.description = "Die Uhrzeit der Inanspruchnahme ist anzugeben."
            entry.rule_texts = ["Die Uhrzeit der Inanspruchnahme ist anzugeben."]
        return entry


class NoisyOphthalmologyCatalog(FakeCatalog):
    def search(self, query: str, quarter: str, limit: int = 25):
        return [
            entry
            for gop in ("06212", "01436", "06310")
            if (entry := self.lookup(gop, quarter)) is not None
        ]


class SearchCatalog(FakeCatalog):
    def search(self, query: str, quarter: str, limit: int = 25):
        if "Augenhintergrund" not in query:
            return []
        return [
            CatalogEntry(
                source="EBM_KBV",
                quarter=quarter,
                catalog_id=f"ebm_kbv_{quarter.lower().replace('/', '_')}",
                catalog_label=f"KBV EBM {quarter}",
                gop="06333",
                gop_base="06333",
                title="Binokulare Untersuchung des Augenhintergrundes",
                points=100,
                euro=12.34,
            )
        ]

    def lookup(self, gop: str, quarter: str, region: str = "Hessen"):
        base, _ = normalize_gop(gop)
        if base == "06333":
            return CatalogEntry(
                source="EBM_KBV",
                quarter=quarter,
                catalog_id=f"ebm_kbv_{quarter.lower().replace('/', '_')}",
                catalog_label=f"KBV EBM {quarter}",
                gop="06333",
                gop_base="06333",
                title="Binokulare Untersuchung des Augenhintergrundes",
                points=100,
                euro=12.34,
            )
        return super().lookup(gop, quarter, region)


class DirectCandidateCatalog(FakeCatalog):
    def lookup(self, gop: str, quarter: str, region: str = "Hessen"):
        base, _ = normalize_gop(gop)
        if base == "06330":
            return CatalogEntry(
                source="EBM_KBV",
                quarter=quarter,
                catalog_id=f"ebm_kbv_{quarter.lower().replace('/', '_')}",
                catalog_label=f"KBV EBM {quarter}",
                gop="06330",
                gop_base="06330",
                title="Perimetrie",
                points=156,
                euro=19.88,
            )
        return super().lookup(gop, quarter, region)


class RegionalCandidateCatalog(FakeCatalog):
    def lookup_hessen(self, gop: str, quarter: str, region: str = "Hessen"):
        base, _ = normalize_gop(gop)
        if base != "01210":
            return None
        return CatalogEntry(
            source="KV_HESSEN_GOP",
            quarter=quarter,
            catalog_id=f"kv_hessen_gop_{quarter.lower().replace('/', '_')}",
            catalog_label=f"KV_HESSEN_GOP {region} {quarter}",
            data_stand="01.04.2026",
            gop="01210H",
            gop_base="01210",
            title="Hessen-Zuschlag Notfall",
            points=26,
            euro=3.21,
            region=region,
            page=7,
        )

    def search(self, query: str, quarter: str, limit: int = 25):
        if "Hessen-Zuschlag" not in query:
            return []
        entry = self.lookup_hessen("01210H", quarter)
        return [entry] if entry else []


def settings() -> Settings:
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


def ev(kind: str, page: int = 1, service_date: Optional[str] = "2025-10-04", service_time: Optional[str] = "00:01") -> Evidence:
    return Evidence(
        evidence_id=f"ev-{kind}",
        kind=kind,
        label=kind,
        page=page,
        service_date=service_date,
        service_time=service_time,
        text=kind,
    )


def clinical_ev(kind: str, page: int = 1) -> Evidence:
    return Evidence(
        evidence_id=f"ev-{kind}",
        kind=kind,
        label="Augenhintergrund / Fundus-Hinweis",
        page=page,
        service_date="2025-10-05",
        service_time="19:37",
        text="Binokulare Untersuchung des Augenhintergrundes dokumentiert",
        metadata={"search_terms": ["Augenhintergrund", "Fundus"]},
    )


def internal_candidate_ev(kind: str, page: int = 1) -> Evidence:
    return Evidence(
        evidence_id=f"ev-{kind}",
        kind=kind,
        label="Interner Hinweis Perimetrie",
        page=page,
        service_date="2026-04-24",
        service_time="12:20",
        text="Interner Leistungsbogen enthält AUA_PERI / Perimetrie",
        metadata={"candidate_gops": ["06330"], "search_terms": ["nicht-treffender Suchtext"]},
    )


def regional_candidate_ev(kind: str, page: int = 1) -> Evidence:
    return Evidence(
        evidence_id=f"ev-{kind}",
        kind=kind,
        label="Hessen-Zuschlag Notfall",
        page=page,
        service_date="2026-04-24",
        service_time="12:20",
        text="Regionaler Hessen-Zuschlag für den Notfallkontakt ist dokumentiert",
        metadata={"search_terms": ["Hessen-Zuschlag"]},
    )


def test_semantic_billing_uses_llm_json_and_catalog_validation():
    evidence = [ev("context.kv_notfall_zna"), ev("lab.creatinine")]

    def fake_llm(messages, _settings):
        assert "catalog_candidates" in messages[1]["content"]
        return {
            "items": [
                {
                    "gop": "01210",
                    "quantity": 1,
                    "evidence_ids": ["ev-context.kv_notfall_zna"],
                    "service_date": "2025-10-04",
                    "service_time": "00:01",
                    "confidence": "high",
                    "reason": "ZNA-Kontakt im KV-Notfalldienst dokumentiert.",
                },
                {
                    "gop": "32066",
                    "quantity": 1,
                    "evidence_ids": ["ev-lab.creatinine"],
                    "confidence": "medium",
                    "reason": "Kreatininwert als Laborleistung dokumentiert.",
                },
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        FakeCatalog(),
        default_quarter="2025/Q4",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01212", "32066"]
    assert result.items[0].derivation_source == "semantic_llm"
    assert result.items[0].semantic_reason == "ZNA-Kontakt im KV-Notfalldienst dokumentiert."
    assert "korrigiert" in result.items[0].validation_notes[0]
    assert result.summary.amount_total_eur == 24.41


def test_semantic_billing_postprocesses_missing_01226_surcharge():
    evidence = [
        ev("context.kv_notfall_zna", service_date="2026-02-25", service_time="01:13"),
        Evidence(
            evidence_id="ev-f03",
            kind="diagnosis.icd10",
            label="ICD-10 F03",
            page=28,
            service_date="2026-02-25",
            service_time="01:11",
            value="F03",
            text="Nicht näher bezeichnete Demenz (H) Nicht näher bezeichnete Demenz (F03)",
            metadata={"icd10": "F03"},
        ),
        Evidence(
            evidence_id="ev-report",
            kind="clinical.domain.neurology",
            label="Neurologie",
            page=11,
            service_date="2026-02-25",
            service_time="01:13",
            text="Alter 81 J. schwere Demenz, zu allen Qualitäten desorientiert.",
        ),
    ]

    def fake_llm(_messages, _settings):
        return {
            "items": [
                {
                    "gop": "1212",
                    "quantity": 1,
                    "evidence_ids": ["ev-context.kv_notfall_zna"],
                    "service_date": "2026-02-25",
                    "service_time": "01:13",
                    "confidence": "high",
                    "reason": "Notfallkontakt nachts.",
                }
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        FakeCatalog(),
        default_quarter="2026/Q1",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01212", "01226"]
    assert result.items[1].derivation_source == "deterministic_rules"
    assert result.summary.amount_total_eur == 35.31


def test_semantic_billing_keeps_01210_for_weekday_daytime():
    evidence = [ev("context.kv_notfall_zna", service_date="2026-04-24", service_time="12:20")]

    def fake_llm(_messages, _settings):
        return {
            "items": [
                {
                    "gop": "01210",
                    "quantity": 1,
                    "evidence_ids": ["ev-context.kv_notfall_zna"],
                    "service_date": "2026-04-24",
                    "service_time": "12:20",
                    "confidence": "high",
                    "reason": "ZNA-Kontakt tagsüber an einem Werktag.",
                }
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        FakeCatalog(),
        default_quarter="2026/Q2",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01210"]
    assert result.items[0].validation_status == "valid"


def test_semantic_billing_does_not_accept_gop_outside_candidate_pool():
    evidence = [ev("context.kv_notfall_zna")]

    def fake_llm(_messages, _settings):
        return {
            "items": [
                {
                    "gop": "99999",
                    "quantity": 1,
                    "evidence_ids": ["ev-context.kv_notfall_zna"],
                    "confidence": "high",
                    "reason": "Halluzinierter Testvorschlag.",
                }
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        FakeCatalog(),
        default_quarter="2025/Q4",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01212"]
    assert result.review_candidates[0].possible_gops == ["99999"]
    assert "Katalog-Kandidatenpool" in result.review_candidates[0].reason


def test_semantic_billing_keeps_only_rule_backed_items_for_ophthalmology_case():
    evidence = [
        ev("context.kv_notfall_zna", page=5, service_date="2026-04-24", service_time="12:20"),
        ev("clinical.ophthalmology_fundus", page=6, service_date="2026-04-24", service_time="11:28"),
        ev(
            "clinical.diagnostics.ophthalmic_sonography",
            page=20,
            service_date="2026-04-24",
            service_time=None,
        ),
    ]

    def fake_llm(_messages, _settings):
        return {
            "items": [
                {
                    "gop": "01210",
                    "evidence_ids": ["ev-context.kv_notfall_zna"],
                    "confidence": "high",
                    "reason": "Notfallkontakt an einem Werktag tagsüber.",
                },
                {
                    "gop": "06333",
                    "evidence_ids": ["ev-clinical.ophthalmology_fundus"],
                    "confidence": "high",
                    "reason": "Binokulare Fundusuntersuchung ist dokumentiert.",
                },
                {
                    "gop": "33000",
                    "evidence_ids": ["ev-clinical.diagnostics.ophthalmic_sonography"],
                    "confidence": "high",
                    "reason": "Durchgeführte Augensonografie ist dokumentiert.",
                },
                {
                    "gop": "06212",
                    "evidence_ids": ["ev-clinical.ophthalmology_fundus"],
                    "confidence": "medium",
                    "reason": "Allgemeiner Katalogtreffer.",
                },
                {
                    "gop": "01436",
                    "evidence_ids": ["ev-clinical.ophthalmology_fundus"],
                    "confidence": "medium",
                    "reason": "Konsultation könnte passen.",
                },
                {
                    "gop": "06310",
                    "evidence_ids": ["ev-clinical.ophthalmology_fundus"],
                    "confidence": "medium",
                    "reason": "Die Voraussetzung von vier Messungen ist nicht vollständig erfüllt.",
                },
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        NoisyOphthalmologyCatalog(),
        default_quarter="2026/Q2",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01210", "06333", "33000"]
    assert {gop for item in result.review_candidates for gop in item.possible_gops} == {
        "01436",
        "06212",
        "06310",
    }


def test_semantic_billing_marks_general_catalog_rule_review_when_time_is_missing():
    evidence = [ev("lab.creatinine", service_date="2026-04-24", service_time=None)]

    def fake_llm(messages, _settings):
        assert "Die Uhrzeit der Inanspruchnahme ist anzugeben." in messages[1]["content"]
        return {
            "items": [
                {
                    "gop": "32066",
                    "quantity": 1,
                    "evidence_ids": ["ev-lab.creatinine"],
                    "service_date": "2026-04-24",
                    "service_time": None,
                    "confidence": "high",
                    "reason": "Kreatininwert als Laborleistung dokumentiert.",
                }
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        RuleTextCatalog(),
        default_quarter="2026/Q2",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["32066"]
    assert result.items[0].validation_status == "review"
    assert any("Uhrzeit" in note for note in result.items[0].validation_notes)


def test_semantic_billing_uses_evidence_metadata_search_terms_for_candidates():
    evidence = [clinical_ev("clinical.ophthalmology_fundus")]

    def fake_llm(messages, _settings):
        assert "06333" in messages[1]["content"]
        return {
            "items": [
                {
                    "gop": "6333",
                    "quantity": 1,
                    "evidence_ids": ["ev-clinical.ophthalmology_fundus"],
                    "confidence": "medium",
                    "reason": "Augenhintergrund-Untersuchung semantisch passend.",
                }
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        SearchCatalog(),
        default_quarter="2025/Q4",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["06333"]
    assert result.items[0].semantic_catalog_candidates == ["cand-001"]


def test_semantic_billing_uses_explicit_metadata_gop_candidates_before_text_search():
    evidence = [internal_candidate_ev("internal_service.aua_peri")]

    def fake_llm(messages, _settings):
        assert "06330" in messages[1]["content"]
        return {
            "items": [
                {
                    "gop": "6330",
                    "quantity": 1,
                    "evidence_ids": ["ev-internal_service.aua_peri"],
                    "confidence": "medium",
                    "reason": "Perimetrie steht als interner Leistungsbogenhinweis bereit.",
                }
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        DirectCandidateCatalog(),
        default_quarter="2026/Q2",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["06330"]
    assert result.items[0].semantic_catalog_candidates == ["cand-001"]


def test_semantic_billing_preserves_regional_catalog_source():
    evidence = [regional_candidate_ev("regional.hessen_notfall_zuschlag")]

    def fake_llm(messages, _settings):
        assert "KV_HESSEN_GOP Hessen 2026/Q2" in messages[1]["content"]
        return {
            "items": [
                {
                    "gop": "01210H",
                    "quantity": 1,
                    "evidence_ids": ["ev-regional.hessen_notfall_zuschlag"],
                    "confidence": "medium",
                    "reason": "Regionaler Hessen-Zuschlag wurde aus der Evidenz abgeleitet.",
                }
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        RegionalCandidateCatalog(),
        default_quarter="2026/Q2",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01210H"]
    assert result.items[0].catalog_source == "KV_HESSEN_GOP"
    assert result.items[0].catalog_source_label == "KV_HESSEN_GOP Hessen 2026/Q2"
    assert result.items[0].catalog_id == "kv_hessen_gop_2026_q2"


def test_semantic_billing_keeps_same_gop_for_separate_service_days():
    evidence = [
        Evidence(
            evidence_id="ev-ctg-1",
            kind="clinical.diagnostics.ctg",
            label="CTG",
            page=11,
            service_date="2026-01-01",
            service_time="13:05",
            text="CTG am ersten Behandlungstag.",
        ),
        Evidence(
            evidence_id="ev-ctg-2",
            kind="clinical.diagnostics.ctg",
            label="CTG",
            page=19,
            service_date="2026-01-03",
            service_time="13:19",
            text="CTG am zweiten Behandlungstag.",
        ),
    ]

    def fake_llm(_messages, _settings):
        return {
            "items": [
                {"gop": "01786", "evidence_ids": ["ev-ctg-1"], "confidence": "high", "reason": "CTG Tag 1"},
                {"gop": "01786", "evidence_ids": ["ev-ctg-2"], "confidence": "high", "reason": "CTG Tag 2"},
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        FakeCatalog(),
        default_quarter="2026/Q1",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01786", "01786"]
    assert [item.service_date for item in result.items] == ["2026-01-01", "2026-01-03"]
    assert result.items[0].service_event_id != result.items[1].service_event_id


def test_semantic_billing_changes_later_emergency_event_to_consultation_family():
    evidence = [
        ev("context.kv_notfall_zna", page=8, service_date="2026-01-01", service_time="13:15"),
        Evidence(
            evidence_id="ev-context.kv_notfall_zna-follow-up",
            kind="context.kv_notfall_zna",
            label="Notfallambulanz Folgekontakt",
            page=17,
            service_date="2026-01-03",
            service_time="13:34",
            text="Weitere Vorstellung in der Notfallambulanz.",
        ),
    ]

    def fake_llm(_messages, _settings):
        return {
            "items": [
                {
                    "gop": "01212",
                    "evidence_ids": ["ev-context.kv_notfall_zna"],
                    "confidence": "high",
                    "reason": "Erster Notfallkontakt.",
                },
                {
                    "gop": "01212",
                    "evidence_ids": ["ev-context.kv_notfall_zna-follow-up"],
                    "confidence": "high",
                    "reason": "Weiterer Notfallkontakt.",
                },
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        FakeCatalog(),
        default_quarter="2026/Q1",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01212", "01216"]
    assert [item.temporal_role for item in result.items] == ["initial_contact", "follow_up_contact"]
    assert result.items[1].temporal_reason
    assert result.items[1].temporal_reason.startswith("Weiterer chronologisch erkannter Kontakt")


def test_semantic_billing_deduplicates_one_notfall_session_across_midnight():
    evidence = [
        Evidence(
            evidence_id="ev-clinical-before-midnight",
            kind="clinical.service.examination",
            label="Klinische Untersuchung",
            page=1,
            service_date="2026-01-29",
            service_time="23:40",
            text="Klinische Untersuchung im laufenden Notfallkontakt.",
        ),
        Evidence(
            evidence_id="ev-notfall-after-midnight",
            kind="context.kv_notfall_zna",
            label="Notfallbehandlung",
            page=2,
            service_date="2026-01-30",
            service_time="00:39",
            text="Dokumentation derselben Notfallbehandlung.",
        ),
    ]

    def fake_llm(_messages, _settings):
        return {
            "items": [
                {
                    "gop": "01212",
                    "evidence_ids": ["ev-notfall-after-midnight"],
                    "service_date": "2026-01-30",
                    "service_time": "00:39",
                    "confidence": "high",
                    "reason": "Notfallkontakt nach Mitternacht.",
                },
                {
                    "gop": "01212",
                    "evidence_ids": ["ev-clinical-before-midnight"],
                    "service_date": "2026-01-29",
                    "service_time": "23:40",
                    "confidence": "high",
                    "reason": "Beginn des Notfallkontakts vor Mitternacht.",
                },
            ],
            "review_candidates": [],
            "excluded_evidence": [],
        }

    result = generate_semantic_billing_items(
        evidence,
        FakeCatalog(),
        default_quarter="2026/Q1",
        settings=settings(),
        llm_client=fake_llm,
    )

    assert [item.gop_original for item in result.items] == ["01212"]
    assert result.items[0].service_date == "2026-01-29"
    assert result.items[0].service_time == "23:40"
    assert result.items[0].temporal_role == "initial_contact"
    assert any("dasselbe zeitliche Kontakt" in item.reason for item in result.review_candidates)
