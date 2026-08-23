import json
from pathlib import Path

from app.billing_rule_definitions import RULE_DEFINITIONS_PATH, load_billing_rule_set
from app.clinical_definitions import CLINICAL_DEFINITIONS_PATH, load_clinical_definition_set
from app.site_definitions import apply_marker_extensions, load_site_definition_set

# Kennungen, die aus dem KIS eines Standorts stammen und weder aus dem EBM-Katalog
# noch aus klinischer Sprache ableitbar sind.
HOUSE_CODE_MARKERS = (
    "racth",
    "racko",
    "rakgk",
    "aua_",
    "all_kongeb",
    "all_ordgeb",
    "all_ordnot",
    "ras9048",
)


def _blob(path: Path) -> str:
    return json.dumps(json.loads(path.read_text(encoding="utf-8")), ensure_ascii=False).casefold()


def test_generic_rule_sets_contain_no_site_service_codes() -> None:
    for path in (CLINICAL_DEFINITIONS_PATH, RULE_DEFINITIONS_PATH):
        blob = _blob(path)
        hits = sorted(marker for marker in HOUSE_CODE_MARKERS if marker in blob)
        assert hits == [], (
            f"Hausinterne Leistungskennungen gehören in site_service_codes.json, "
            f"nicht in {path.name}: {', '.join(hits)}"
        )


def test_site_layer_restores_codes_and_candidates() -> None:
    site = load_site_definition_set()
    assert not site.empty

    merged = load_clinical_definition_set()
    internal = [rule for rule in merged.evidence_rules if rule["kind"].startswith("internal_service")]
    assert len(internal) == len(site.evidence_rules)

    by_id = {rule["rule_id"]: rule for rule in merged.evidence_rules}
    extended = json.dumps(by_id["evidence.radiology.ct_head.v1"], ensure_ascii=False).casefold()
    assert "racth" in extended, "Standortmarker wurde nicht eingemischt"
    assert "ctkopfnativ" in extended, "klinischer Marker ging verloren"

    rule_set = load_billing_rule_set()
    kinds = {rule.evidence_kind for rule in rule_set.candidate_rules}
    assert "internal_service.aua_echo" in kinds


def test_system_runs_without_a_site_file() -> None:
    """Ein Deployment ohne Standortdatei muss laden, nur ohne Hauscodes."""
    load_clinical_definition_set.cache_clear()
    load_billing_rule_set.cache_clear()
    load_site_definition_set.cache_clear()
    try:
        bare = load_clinical_definition_set(site_path="/nicht/vorhanden.json")
        bare_rules = load_billing_rule_set(site_path="/nicht/vorhanden.json")
        assert bare.evidence_rules
        assert not [r for r in bare.evidence_rules if r["kind"].startswith("internal_service")]
        assert not [r for r in bare_rules.candidate_rules if r.evidence_kind.startswith("internal_service")]
    finally:
        load_clinical_definition_set.cache_clear()
        load_billing_rule_set.cache_clear()
        load_site_definition_set.cache_clear()


def test_marker_extension_never_touches_a_negated_branch() -> None:
    """Ein Standortmarker darf eine Ausschlussbedingung nicht umkehren."""
    rules = [
        {
            "rule_id": "r1",
            "when": {
                "all": [
                    {"text_any": ["befund"]},
                    {"not": {"text_any": ["storniert"]}},
                ]
            },
        }
    ]

    merged = apply_marker_extensions(rules, {"r1": {"text_any": ["HAUSCODE"]}})

    positive = merged[0]["when"]["all"][0]["text_any"]
    negated = merged[0]["when"]["all"][1]["not"]["text_any"]
    assert positive == ["befund", "HAUSCODE"]
    assert negated == ["storniert"]


def test_marker_extension_reaches_every_branch() -> None:
    rules = [
        {
            "rule_id": "r1",
            "when": {"any": [{"text_any": ["a"]}, {"all": [{"text_any": ["b"]}]}]},
        }
    ]

    merged = apply_marker_extensions(rules, {"r1": {"text_any": ["X"]}})

    assert merged[0]["when"]["any"][0]["text_any"] == ["a", "X"]
    assert merged[0]["when"]["any"][1]["all"][0]["text_any"] == ["b", "X"]
