from app.document_segmentation import segment_pages
from app.billing_rules import candidate_gops_for_evidence_kind
from app.clinical_definitions import load_clinical_definition_set
from app.evidence_extraction import extract_evidence
from app.models import PageText
from app.selection_extraction import extract_selection_entries_from_text


def _page_with_selections(page: int, text: str) -> PageText:
    definitions = load_clinical_definition_set()
    return PageText(
        page=page,
        text=text,
        selection_entries=extract_selection_entries_from_text(text, definitions.selection_extraction),
    )


def test_ophthalmology_emergency_fundus_and_performed_sonography_are_distinguished():
    pages = [
        PageText(
            page=1,
            text=(
                "Ambulanz Augen - Befund Termin dgf:24.04.202612:20Uhr "
                "Die Patientin stellt sich bei uns als Notfall vor: Glaskörperblutung RA."
            ),
        ),
        PageText(
            page=2,
            text=(
                "Glaskörperblutung. Hinterer RA Netzhaut liegt an Augenabschnitt. "
                "Fundus teilweise sichtbar, Papille und Makula beurteilbar."
            ),
        ),
        PageText(
            page=3,
            text=(
                "ACCUTOME Ophthalmic Ultrasound Scan Date: 4/24/2026 "
                "Probe Freq: 12 MHz Max Depth: 60 mm Gain: 57 dB OD"
            ),
        ),
        PageText(
            page=4,
            text=(
                "Eingelesenes Dokument Status fertig Dokumententyp Diagnostik, Sonographie "
                "Kategorie GK-Blutung"
            ),
        ),
    ]

    segments = segment_pages(pages)
    evidence, _review, _excluded, context = extract_evidence(pages, segments)
    selected = {
        item.kind: item
        for item in evidence
        if item.kind
        in {
            "context.kv_notfall_zna",
            "clinical.ophthalmology_fundus",
            "clinical.diagnostics.ophthalmic_sonography",
        }
    }

    assert [segment.segment_type for segment in segments] == ["treatment_report", "ophthalmic_sonography"]
    assert set(selected) == {
        "context.kv_notfall_zna",
        "clinical.ophthalmology_fundus",
        "clinical.diagnostics.ophthalmic_sonography",
    }
    assert selected["context.kv_notfall_zna"].service_time == "12:20"
    assert selected["clinical.diagnostics.ophthalmic_sonography"].service_date == "2026-04-24"
    assert context["quarter"] == "2026/Q2"


def test_emergency_timeline_distinguishes_admission_triage_and_physician_contact():
    pages = [
        PageText(
            page=1,
            text=(
                "Behandlungsbericht ZNA Notfall. Aufnahme 24.04.2026 18:50. "
                "Triage 24.04.2026 18:57. Erstkontakt Arzt 24.04.2026 19:05."
            ),
        )
    ]

    segments = segment_pages(pages)
    evidence, review, _excluded, context = extract_evidence(pages, segments)
    selected = {item.kind: item for item in evidence if item.kind.startswith(("timeline.", "context.kv_"))}

    assert selected["timeline.administrative_admission"].service_time == "18:50"
    assert selected["timeline.triage"].service_time == "18:57"
    assert selected["context.kv_notfall_zna"].service_time == "19:05"
    assert context["administrative_admission"] == "2026-04-24T18:50:00"
    assert context["first_personal_physician_contact"] == "2026-04-24T19:05:00"
    assert context["treatment_start"] == "2026-04-24T19:05:00"
    assert not any("ohne sicher erkannten" in item.evidence for item in review)


def test_emergency_admission_without_physician_contact_is_review_only():
    pages = [
        PageText(
            page=1,
            text="Behandlungsbericht ZNA Notfall. Aufnahme 24.04.2026 18:50.",
        )
    ]

    segments = segment_pages(pages)
    evidence, review, _excluded, context = extract_evidence(pages, segments)

    assert any(item.kind == "timeline.administrative_admission" for item in evidence)
    assert not any(item.kind == "context.kv_notfall_zna" for item in evidence)
    assert context["administrative_admission"] == "2026-04-24T18:50:00"
    assert context["first_personal_physician_contact"] is None
    assert any("ohne sicher erkannten" in item.evidence for item in review)
    assert any("01210" in item.possible_gops for item in review)


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
    assert all("candidate_gops" not in item.metadata for item in evidence_by_kind.values())
    # Die GOP kommt nicht mehr aus einer konfigurierten Zuordnung, sondern aus der
    # Katalogsuche. Das Regelwerk kennt fuer diese Evidenzart keine Kandidaten.
    assert candidate_gops_for_evidence_kind("radiology.xray_extremities") == []
    assert candidate_gops_for_evidence_kind("radiology.xray_hand_foot") == []
    assert candidate_gops_for_evidence_kind("radiology.ct_hand_foot") == []
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


def test_abdominal_sonography_inherits_date_from_contiguous_report_page():
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
    renal = next(item for item in evidence if item.kind == "clinical.diagnostics.abdominal_sonography")

    assert renal.service_date == "2026-01-01"
    assert "candidate_gops" not in renal.metadata
    assert candidate_gops_for_evidence_kind(renal.kind) == []
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
