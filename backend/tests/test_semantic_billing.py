from pathlib import Path

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
            "32066": ("Kreatinin", None, 0.25),
        }
        base, _ = normalize_gop(gop)
        if base not in values:
            return None
        title, points, euro = values[base]
        return CatalogEntry(source="EBM_KBV", quarter=quarter, gop=base, gop_base=base, title=title, points=points, euro=euro)

    def search(self, query: str, quarter: str, limit: int = 25):
        return []


class SearchCatalog(FakeCatalog):
    def search(self, query: str, quarter: str, limit: int = 25):
        if "Augenhintergrund" not in query:
            return []
        return [
            CatalogEntry(
                source="EBM_KBV",
                quarter=quarter,
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
                gop="06333",
                gop_base="06333",
                title="Binokulare Untersuchung des Augenhintergrundes",
                points=100,
                euro=12.34,
            )
        return super().lookup(gop, quarter, region)


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


def ev(kind: str, page: int = 1) -> Evidence:
    return Evidence(
        evidence_id=f"ev-{kind}",
        kind=kind,
        label=kind,
        page=page,
        service_date="2025-10-04",
        service_time="00:01",
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

    assert [item.gop_original for item in result.items] == ["01210", "32066"]
    assert result.items[0].derivation_source == "semantic_llm"
    assert result.items[0].semantic_reason == "ZNA-Kontakt im KV-Notfalldienst dokumentiert."
    assert result.summary.amount_total_eur == 15.12


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

    assert result.items == []
    assert result.review_candidates[0].possible_gops == ["99999"]
    assert "Katalog-Kandidatenpool" in result.review_candidates[0].reason


def test_semantic_billing_uses_evidence_metadata_search_terms_for_candidates():
    evidence = [clinical_ev("clinical.ophthalmology_fundus")]

    def fake_llm(messages, _settings):
        assert "06333" in messages[1]["content"]
        return {
            "items": [
                {
                    "gop": "06333",
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
