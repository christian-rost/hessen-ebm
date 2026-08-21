from app.clinical_definitions import load_clinical_definition_set
from app.document_segmentation import segment_pages
from app.evidence_extraction import extract_evidence
from app.models import PageText
from app.selection_extraction import (
    extract_selection_entries_from_pdf_page,
    extract_selection_entries_from_text,
)


class _VectorPage:
    rects = [
        {"x0": 20, "x1": 28, "top": 10, "bottom": 18, "width": 8, "height": 8},
        {"x0": 20, "x1": 28, "top": 50, "bottom": 58, "width": 8, "height": 8},
    ]
    curves = [
        {
            "x0": 20,
            "x1": 28,
            "top": 30,
            "bottom": 38,
            "width": 8,
            "height": 8,
            "pts": [(20, 30), (28, 30), (28, 38), (20, 38), (20, 30), (28, 38)],
        }
    ]
    lines = []

    def extract_words(self, **_kwargs):
        return [
            {"text": "1.00", "x0": 40, "top": 9, "bottom": 19},
            {"text": "CFG_ALPHA", "x0": 65, "top": 9, "bottom": 19},
            {"text": "Erste", "x0": 130, "top": 9, "bottom": 19},
            {"text": "1.00", "x0": 40, "top": 29, "bottom": 39},
            {"text": "CFG_BETA", "x0": 65, "top": 29, "bottom": 39},
            {"text": "Zweite", "x0": 130, "top": 29, "bottom": 39},
            {"text": "1.00", "x0": 40, "top": 49, "bottom": 59},
            {"text": "CFG_GAMMA", "x0": 65, "top": 49, "bottom": 59},
            {"text": "Dritte", "x0": 130, "top": 49, "bottom": 59},
        ]


def test_text_selection_parser_distinguishes_checked_unchecked_and_tree_controls():
    configuration = load_clinical_definition_set().selection_extraction
    text = """Leistungsbogen
⊟ Augenambulanz
⊞ IVOM
☐ 1.00CFG_ALPHA Erste Leistung
☒ 1.00CFG_BETA Zweite Leistung
[ ] 1.00CFG_GAMMA Dritte Leistung
"""

    entries = extract_selection_entries_from_text(text, configuration)

    assert [(entry.code, entry.state) for entry in entries] == [
        ("CFG_ALPHA", "unchecked"),
        ("CFG_BETA", "checked"),
        ("CFG_GAMMA", "unchecked"),
    ]
    assert all(entry.quantity == 1.0 for entry in entries)


def test_pdf_vector_parser_associates_checkbox_geometry_with_its_row():
    configuration = load_clinical_definition_set().selection_extraction

    entries = extract_selection_entries_from_pdf_page(
        _VectorPage(),
        "Leistungsbogen CFG_ALPHA CFG_BETA CFG_GAMMA",
        configuration,
    )

    assert {(entry.code, entry.state) for entry in entries} == {
        ("CFG_ALPHA", "unchecked"),
        ("CFG_BETA", "checked"),
        ("CFG_GAMMA", "unchecked"),
    }
    assert next(entry for entry in entries if entry.code == "CFG_BETA").bbox == (20.0, 30.0, 28.0, 38.0)


def test_only_checked_service_list_rows_create_internal_evidence():
    definitions = load_clinical_definition_set()
    text = """Datenerfassung Durchgeführte Leistungen Leistungsbogen
☒ 1.00ALL_KONGEB Konsultationsgebühr
☐ 1.00ALL_ORDGEB Ordinationsgebühr
☐ 1.00AUA_ECHO Echographie
"""
    page = PageText(
        page=1,
        text=text,
        selection_entries=extract_selection_entries_from_text(text, definitions.selection_extraction),
    )

    evidence, review, excluded, _context = extract_evidence(
        [page],
        segment_pages([page], definitions),
        definitions,
    )

    internal = [item for item in evidence if item.kind.startswith("internal_service.")]
    assert [item.kind for item in internal] == ["internal_service.consultation_fee"]
    assert internal[0].metadata["selection_entry"]["code"] == "ALL_KONGEB"
    assert len([item for item in review if "Leistungsbogenhinweis" in item.reason]) == 1
    assert excluded == []


def test_unmarked_ocr_rows_are_reviewed_instead_of_billed():
    definitions = load_clinical_definition_set()
    text = """Datenerfassung Durchgeführte Leistungen Leistungsbogen
1.00ALL_KONGEB Konsultationsgebühr
1.00AUA_ECHO Echographie
"""
    entries = extract_selection_entries_from_text(text, definitions.selection_extraction)
    page = PageText(page=1, text=text, provider="mistral_ocr", selection_entries=entries)

    evidence, review, _excluded, _context = extract_evidence(
        [page],
        segment_pages([page], definitions),
        definitions,
    )

    assert {entry.state for entry in entries} == {"ambiguous"}
    assert not any(item.kind.startswith("internal_service.") for item in evidence)
    assert {item.evidence for item in review if "Auswahlzustand" in item.reason} == {
        "Unklar markierter Listeneintrag: ALL_KONGEB Konsultationsgebühr",
        "Unklar markierter Listeneintrag: AUA_ECHO Echographie",
    }
