from app.document_segmentation import segment_pages
from app.evidence_extraction import extract_evidence
from app.models import PageText


def test_ophthalmology_ambulance_pages_create_clinical_evidence():
    pages = [
        PageText(
            page=1,
            text=(
                "Musterklinik Fall-Nr. FALL-D "
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
    assert "clinical.domain.dermatology" not in kinds
    assert "internal_service.emergency_ordination" in kinds
    assert "internal_service.ophthalmology_fundus" in kinds
    assert all(item.service_date != "1964-11-08" for item in evidence)
    assert any("01210" in item.possible_gops for item in review)
    assert any("06333" in item.possible_gops for item in review)
    assert excluded == []


def test_ophthalmology_data_capture_continuation_creates_all_internal_hints():
    pages = [
        PageText(
            page=1,
            text=(
                "Behandlungsvertrag ueber Krankenhausleistungen "
                "Patienten-Identifikationsarmband bei Notfallbehandlung"
            ),
        ),
        PageText(
            page=2,
            text=(
                "Musterklinik Datenerfassung Durchgefuehrte Leistungen "
                "1.Leistung am24.04.2026 um 12:20 Dauer min. Bereitschaftsdienst "
                "Leistungsbogen(9080902 Institutsambul. Augenklinik) "
                "1.00ALL_KONGEB Konsultationsgebuehr "
                "1.00ALL_ORDGEB Ordinationsgebuehr "
                "1.00ALL_ORDNOT Ordinationsgebuehr(Notfall) "
                "1.00AUA_BUAHG Binokulare Untersuchung des Augenhintergrundes "
                "1.00AUA_ECHO Echographie "
                "1.00AUA_EPU Elektrophysiologische Untersuchung "
                "1.00AUA_FAG Fluoreszenzangiographie "
                "1.00AUA_LIDHEB"
            ),
        ),
        PageText(
            page=3,
            text=(
                "Durchgefuehrte Leistungen OP der Lidsenkung mit Lidheber "
                "1.00AUA_PDT PDT "
                "1.00AUA_PERI Perimetrie "
                "1.00AUA_SCHIEL Quant. Untersuchung des binokularen Sehens "
                "1.00ERG ERG "
                "1.00TWS TW-Sondierung "
                "1.00VEP VEP "
                "Privatliquidation Selbstzahler/Notfaelle Augenambulanz&EBM Prozeduren"
            ),
        ),
    ]

    segments = segment_pages(pages)
    evidence, review, excluded, _context = extract_evidence(pages, segments)
    kinds = {item.kind for item in evidence}
    metadata_by_kind = {item.kind: item.metadata for item in evidence}

    assert [segment.segment_type for segment in segments] == ["other", "data_capture"]
    assert segments[1].relevant_for_billing is True
    assert "clinical.domain.pulmonology" not in kinds
    assert {
        "internal_service.consultation_fee",
        "internal_service.ordination_fee",
        "internal_service.emergency_ordination",
        "internal_service.ophthalmology_fundus",
        "internal_service.aua_echo",
        "internal_service.aua_epu",
        "internal_service.aua_fag",
        "internal_service.aua_lidheber",
        "internal_service.aua_pdt",
        "internal_service.aua_peri",
        "internal_service.aua_schiel",
        "internal_service.aua_tws",
    }.issubset(kinds)
    assert metadata_by_kind["internal_service.ophthalmology_fundus"]["candidate_gops"] == ["06333"]
    assert metadata_by_kind["internal_service.aua_peri"]["candidate_gops"] == ["06330"]
    assert metadata_by_kind["internal_service.aua_pdt"]["candidate_gops"] == ["06332"]
    assert any("06331" in item.possible_gops for item in review)
    assert excluded == []


def test_ecg_pages_create_semantic_evidence():
    pages = [
        PageText(
            page=1,
            text=(
                "Musterklinik Fall-Nr. FALL-B "
                "Standard 12 Ableitungen EKG Durchgefuehrt 04.10.2025 um 00:31 "
                "Sinusrhythmus, keine akuten Ischaemiezeichen"
            ),
        )
    ]

    segments = segment_pages(pages)
    evidence, review, excluded, _context = extract_evidence(pages, segments)
    evidence_by_kind = {item.kind: item for item in evidence}

    assert len(segments) == 1
    assert segments[0].segment_type == "ecg"
    assert segments[0].relevant_for_billing is True
    assert "clinical.ecg_12_lead" in evidence_by_kind
    assert "clinical.ecg_rhythm_findings" in evidence_by_kind
    assert evidence_by_kind["clinical.ecg_12_lead"].service_date == "2025-10-04"
    assert evidence_by_kind["clinical.ecg_12_lead"].service_time == "00:31"
    assert "12-Kanal-EKG" in evidence_by_kind["clinical.ecg_12_lead"].metadata["search_terms"]
    assert review == []
    assert excluded == []


def test_icd_detection_handles_three_character_codes_and_avoids_lab_false_positives():
    pages = [
        PageText(
            page=1,
            text=(
                "Behandlungsbericht ZNA Diagnose: Exazerbation einer bekannten schweren Demenz. "
                "Aufnahmediagnosen Nicht näher bezeichnete Demenz (H) "
                "Nicht näher bezeichnete Demenz (F03) Erfasst am 25.02.2026 / 01:11 "
                "Alter 81 J."
            ),
        ),
        PageText(
            page=2,
            text=(
                "Laborbefund Hämoglobin 12.7 g/dl Natrium 140 mmol/l "
                "Kalium 3.9 mmol/l Kreatinin 0.77 mg/dl"
            ),
        ),
    ]

    segments = segment_pages(pages)
    evidence, _review, _excluded, context = extract_evidence(pages, segments)
    diagnoses = [item for item in evidence if item.kind == "diagnosis.icd10"]

    assert context["diagnosis"] == "F03"
    assert [item.value for item in diagnoses] == ["F03"]


def test_radiology_hand_forearm_and_wrist_ct_create_billable_evidence():
    pages = [
        PageText(
            page=1,
            text=(
                "Klinik für Radiologie, Neuroradiologie und Nuklearmedizin "
                "Radiologie - Befund "
                "RöntgenUnterarmmitHandgelenkrechts,durchgeführtam17.02.2026um13:46 "
                "RöntgenHandrechts,durchgeführtam17.02.2026um13:36 "
                "KeinNachweiseinerfrischenFrakturoderLuxationalsTraumafolge."
            ),
        ),
        PageText(
            page=2,
            text=(
                "Radiologie - Befund "
                "CTHandgelenkrechts,nativ,durchgeführtam17.02.2026um15:23 "
                "Handgelenk,OssaCarpaliaundteilerfassteOssaMetacarpaliarechtsohneNachweis"
            ),
        ),
    ]

    segments = segment_pages(pages)
    evidence, review, excluded, context = extract_evidence(pages, segments)
    evidence_by_kind = {item.kind: item for item in evidence}

    assert len(segments) == 1
    assert segments[0].segment_type == "radiology_report"
    assert context["quarter"] == "2026/Q1"
    assert evidence_by_kind["radiology.xray_extremities"].metadata["candidate_gops"] == ["34233"]
    assert evidence_by_kind["radiology.xray_hand_foot"].metadata["candidate_gops"] == ["34232"]
    assert evidence_by_kind["radiology.ct_hand_foot"].metadata["candidate_gops"] == ["34351"]
    assert evidence_by_kind["radiology.ct_hand_foot"].service_time == "15:23"
    assert review == []
    assert excluded == []


def test_obstetric_gynecology_pages_create_generic_semantic_evidence():
    pages = [
        PageText(
            page=1,
            text=(
                "Klinik fuer Gynaekologie und Geburtshilfe Frau Hara Sohn geb.: 03.05.1995 "
                "wir berichten Ihnen ueber Hara Sohn, die sich am 24.02.2026 in ambulanter "
                "Behandlung befand. Anamnese Wundheilungsstoerung nach Dammriss IIIA mit "
                "V.a. perianalen Granulationspolypen. Procedere Indikation zur operativen "
                "Abtragung des Granulationspolypen."
            ),
        ),
        PageText(
            page=2,
            text=(
                "Verlaufskontrolle 01.01.2026 Indikation Kontrolle am Termin "
                "Einlingsschwangerschaft Fetale Biometrie BPD 97,1mm AU 320,4mm "
                "Dopplersonographie Arteria umbilicalis unauffaellig CTG alle 2 Tage."
            ),
        ),
        PageText(
            page=3,
            text=(
                "Jeon, Hara - 03.05.1995 CTG-Beurteilung: Normal "
                "01.01.2026 (Start: 13:05 Uhr, Dauer: 0 h 21 min)"
            ),
        ),
    ]

    segments = segment_pages(pages)
    evidence, review, excluded, context = extract_evidence(pages, segments)
    kinds = {item.kind for item in evidence}

    assert [segment.segment_type for segment in segments] == ["gynecology_obstetrics", "ctg"]
    assert context["treatment_start"] == "2026-02-24T00:00:00"
    assert context["quarter"] == "2026/Q1"
    assert "clinical.domain.gynecology_obstetrics" in kinds
    assert "clinical.diagnostics.sonography" in kinds
    assert "clinical.diagnostics.doppler_sonography" in kinds
    assert "clinical.diagnostics.ctg" in kinds
    assert "clinical.diagnostics.prenatal_biometry" in kinds
    assert "clinical.procedure.wound_or_minor_surgery" in kinds
    assert all(item.service_date != "1995-05-03" for item in evidence)
    assert review == []
    assert excluded == []


def test_maternal_renal_sonography_inherits_date_from_contiguous_report_page():
    pages = [
        PageText(
            page=1,
            text=(
                "Klinik für Gynäkologie und Geburtshilfe Verlaufskontrolle am 01.01.2026. "
                "Einlingsschwangerschaft, fetale Biometrie und Sonografie."
            ),
        ),
        PageText(
            page=2,
            text=(
                "Klinik für Gynäkologie und Geburtshilfe, Fortsetzung Sonografie. "
                "Mütterliche Nieren: rechts Hydronephrose Grad II, links unauffällig."
            ),
        ),
    ]

    segments = segment_pages(pages)
    evidence, _review, _excluded, _context = extract_evidence(pages, segments)
    renal = next(item for item in evidence if item.kind == "clinical.diagnostics.maternal_renal_sonography")

    assert renal.service_date == "2026-01-01"
    assert renal.metadata["candidate_gops"] == ["33042"]
    assert renal.metadata["service_datetime_carried_from_previous_page"] is True


def test_major_ebm_domains_are_segmented_as_billing_relevant():
    pages = [
        PageText(page=1, text="Kardiologie Echokardiographie und Herzkatheter Befund am 04.01.2026"),
        PageText(page=2, text="Pneumologie Lungenfunktion Spirometrie bei COPD am 05.01.2026"),
        PageText(page=3, text="Gastroenterologie Koloskopie Endoskopie Befund am 06.01.2026"),
        PageText(page=4, text="Urologie Uroflow und Zystoskopie bei Harnstau am 07.01.2026"),
        PageText(page=5, text="Dermatologie Dermatoskopie Haut Naevus Exzision am 08.01.2026"),
        PageText(page=6, text="HNO Audiometrie Tympanometrie Laryngoskopie am 09.01.2026"),
        PageText(page=7, text="Neurologie EEG EMG NLG bei Parese am 10.01.2026"),
        PageText(page=8, text="Psychiatrie Psychotherapie Depression Gespraech am 11.01.2026"),
        PageText(page=9, text="Orthopaedie Unfallchirurgie Fraktur Gelenk Trauma am 12.01.2026"),
        PageText(page=10, text="Onkologie Haematologie Tumor Chemotherapie am 13.01.2026"),
        PageText(page=11, text="Nephrologie Dialyse Haemodialyse Niereninsuffizienz am 14.01.2026"),
        PageText(page=12, text="Impfung Vorsorge Frueherkennung Screening DMP am 15.01.2026"),
    ]

    segments = segment_pages(pages)
    evidence, _review, _excluded, context = extract_evidence(pages, segments)
    segment_types = {segment.segment_type for segment in segments}
    kinds = {item.kind for item in evidence}

    assert all(segment.relevant_for_billing for segment in segments)
    assert {
        "cardiology_report",
        "pulmonology_report",
        "gastroenterology_report",
        "urology_report",
        "dermatology_report",
        "ent_report",
        "neurology_report",
        "psychiatry_report",
        "orthopedics_report",
        "oncology_report",
        "nephrology_report",
        "prevention_report",
    }.issubset(segment_types)
    assert {
        "clinical.domain.cardiology",
        "clinical.domain.pulmonology",
        "clinical.domain.gastroenterology",
        "clinical.domain.urology",
        "clinical.domain.dermatology",
        "clinical.domain.ent",
        "clinical.domain.neurology",
        "clinical.domain.psychiatry",
        "clinical.domain.orthopedics_trauma",
        "clinical.domain.oncology_hematology",
        "clinical.domain.nephrology_dialysis",
        "clinical.domain.prevention_vaccination",
    }.issubset(kinds)
    assert context["quarter"] == "2026/Q1"
