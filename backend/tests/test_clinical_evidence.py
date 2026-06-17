from app.document_segmentation import segment_pages
from app.evidence_extraction import extract_evidence
from app.models import PageText


def test_ophthalmology_ambulance_pages_create_clinical_evidence():
    pages = [
        PageText(
            page=1,
            text=(
                "Klinikum Frankfurt Hoechst Fall-Nr. 25124749 "
                "Ambulanz Augen - Befund Termin dgf:05.10.202519:37Uhr "
                "Notfall-symptomorientierte Untersuchung Visus RA sc 0,5 "
                "Tensio RA palpatorisch Notfallambulanz Augenklinik"
            ),
        ),
        PageText(
            page=2,
            text=(
                "LSTM-2025-099706, Pat.: Jung, Hans-Joerg, *08.11.1964 "
                "Vorderer Augenabschnitt: dendritischer Epitheldefekt "
                "Hinterer Augenabschnitt: Netzhaut zentral anliegend "
                "Diagnose: RA Herpeskeratitis (B00.5,H19.1) "
                "Befundet am 05.10.2025 16:25"
            ),
        ),
        PageText(
            page=3,
            text=(
                "Datenerfassung Durchgefuehrte Leistungen "
                "1.Leistung am05.10.2025 um 19:37 Dauer min. Bereitschaftsdienst "
                "1.00ALL_ORDNOT Ordinationsgebuehr (Notfall) "
                "1.00AUA_BUAHG Binokulare Untersuchung des Augenhintergrundes"
            ),
        ),
        PageText(
            page=4,
            text=(
                "Ambulanz Augen - Anforderung Status: angefordert "
                "Auftragsdatum 05.10.2025 16:25 Leistung AmbulanzAugen Anzahl 1"
            ),
        ),
    ]

    segments = segment_pages(pages)
    evidence, review, excluded, context = extract_evidence(pages, segments)
    kinds = {item.kind for item in evidence}

    assert [segment.segment_type for segment in segments] == ["treatment_report", "data_capture", "request"]
    assert context["treatment_start"] == "2025-10-05T19:37:00"
    assert context["quarter"] == "2025/Q4"
    assert context["diagnosis"] == "B00.5"
    assert "context.specialty_ambulance_emergency" in kinds
    assert "clinical.ophthalmology_exam" in kinds
    assert "clinical.ophthalmology_fundus" in kinds
    assert "diagnosis.icd10" in kinds
    assert "internal_service.emergency_ordination" in kinds
    assert "internal_service.ophthalmology_fundus" in kinds
    assert all(item.service_date != "1964-11-08" for item in evidence)
    assert review == []
    assert excluded == []
