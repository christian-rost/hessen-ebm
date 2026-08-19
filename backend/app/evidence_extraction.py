from __future__ import annotations

import hashlib
import re
import unicodedata

from .models import DocumentSegment, Evidence, ExcludedEvidence, PageText, ReviewCandidate


DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
TIME_RE = re.compile(r"(\d{2}:\d{2})")
ICD_RE = re.compile(r"([A-Z]\d{2}\.\d{1,2})(?![\d.])")

CLINICAL_CONTEXT_SEGMENTS = {
    "case_context",
    "treatment_report",
    "radiology_report",
    "laboratory_result",
    "ecg",
    "ctg",
    "clinical_report",
    "cardiology_report",
    "pulmonology_report",
    "gastroenterology_report",
    "gynecology_obstetrics",
    "urology_report",
    "dermatology_report",
    "ent_report",
    "neurology_report",
    "psychiatry_report",
    "orthopedics_report",
    "pediatrics_report",
    "surgery_report",
    "anesthesia_report",
    "pathology_report",
    "oncology_report",
    "nephrology_report",
    "prevention_report",
    "therapy_report",
}

DOMAIN_EVIDENCE: list[tuple[str, str, tuple[str, ...], tuple[str, ...], float]] = [
    (
        "clinical.domain.emergency",
        "Akut-/Notfallkontakt",
        ("notfall", "notaufnahme", "notfallambulanz", "zna", "akutambulanz"),
        ("Notfallpauschale", "Notfall", "Akutbehandlung", "Grundpauschale"),
        0.76,
    ),
    (
        "clinical.domain.ophthalmology",
        "Augenheilkunde",
        ("augenklinik", "augenambulanz", "augenheilkunde", "visus", "tensio", "vordereraugenabschnitt", "hintereraugenabschnitt"),
        ("Augenheilkunde", "Augenärztliche Untersuchung", "Visus", "Fundus"),
        0.76,
    ),
    (
        "clinical.domain.gynecology_obstetrics",
        "Gynäkologie / Geburtshilfe",
        ("gynakologie", "geburtshilfe", "frauenklinik", "schwangerschaft", "ssw", "fetale", "pranatal", "vaginal"),
        ("Schwangerschaft", "Geburtshilfe", "Gynäkologie", "Betreuung einer Schwangeren", "Pränataldiagnostik"),
        0.8,
    ),
    (
        "clinical.domain.cardiology",
        "Kardiologie",
        ("kardiologie", "herz", "koronar", "echokardiographie", "herzkatheter", "schrittmacher", "ekg"),
        ("Kardiologie", "EKG", "Echokardiographie", "Herzkatheter", "Schrittmacher"),
        0.76,
    ),
    (
        "clinical.domain.pulmonology",
        "Pneumologie",
        ("pneumologie", "lunge", "bronchoskopie", "spirometrie", "lungenfunktion", "asthma", "copd"),
        ("Pneumologie", "Lungenfunktion", "Spirometrie", "Bronchoskopie", "Asthma"),
        0.74,
    ),
    (
        "clinical.domain.gastroenterology",
        "Gastroenterologie / Endoskopie",
        ("gastroenterologie", "gastroskopie", "koloskopie", "coloskopie", "rektoskopie", "endoskopie", "abdomen"),
        ("Gastroenterologie", "Endoskopie", "Gastroskopie", "Koloskopie", "Abdomensonographie"),
        0.74,
    ),
    (
        "clinical.domain.urology",
        "Urologie",
        ("urologie", "prostata", "harnblase", "zystoskopie", "cystoskopie", "uroflow", "harnstau", "niere"),
        ("Urologie", "Prostata", "Uroflow", "Zystoskopie", "Sonographie Niere"),
        0.74,
    ),
    (
        "clinical.domain.dermatology",
        "Dermatologie",
        ("dermatologie", "haut", "naevus", "melanom", "ekzem", "psoriasis", "dermatoskopie"),
        ("Dermatologie", "Haut", "Dermatoskopie", "Exzision", "Biopsie"),
        0.72,
    ),
    (
        "clinical.domain.ent",
        "HNO",
        ("hno", "halsnasenohren", "audiometrie", "tympanometrie", "laryngoskopie", "tonsillen", "kehlkopf"),
        ("HNO", "Audiometrie", "Tympanometrie", "Laryngoskopie"),
        0.72,
    ),
    (
        "clinical.domain.neurology",
        "Neurologie",
        ("neurologie", "eeg", "emg", "nlg", "epilepsie", "schlaganfall", "parese", "parkinson"),
        ("Neurologie", "EEG", "EMG", "Nervenleitgeschwindigkeit", "Schlaganfall"),
        0.72,
    ),
    (
        "clinical.domain.psychiatry",
        "Psychiatrie / Psychotherapie",
        ("psychiatrie", "psychotherapie", "psychosomatik", "depression", "angststorung", "sucht"),
        ("Psychiatrie", "Psychotherapie", "Psychosomatik", "Gespräch", "Testverfahren"),
        0.7,
    ),
    (
        "clinical.domain.orthopedics_trauma",
        "Orthopädie / Unfallchirurgie",
        ("orthopadie", "orthopaedie", "unfallchirurgie", "fraktur", "luxation", "gelenk", "wirbelsaule", "trauma"),
        ("Orthopädie", "Unfallchirurgie", "Fraktur", "Gelenk", "Wundversorgung"),
        0.72,
    ),
    (
        "clinical.domain.pediatrics",
        "Pädiatrie",
        ("paediatrie", "padiatrie", "kinderklinik", "jugendmedizin", "neugeboren", "saugling"),
        ("Pädiatrie", "Kinder- und Jugendmedizin", "U-Untersuchung", "Neugeborene"),
        0.72,
    ),
    (
        "clinical.domain.surgery",
        "Chirurgie / OP",
        ("chirurgie", "opbericht", "operationsbericht", "operation", "eingriff", "wundversorgung", "naht", "exzision", "abtragung"),
        ("Chirurgie", "Operation", "Wundversorgung", "Exzision", "Naht"),
        0.72,
    ),
    (
        "clinical.domain.anesthesia_pain",
        "Anästhesie / Schmerztherapie",
        ("anasthesie", "anaesthesie", "narkose", "schmerztherapie", "palliativ", "sedierung"),
        ("Anästhesie", "Narkose", "Schmerztherapie", "Palliativmedizin"),
        0.7,
    ),
    (
        "clinical.domain.pathology_cytology",
        "Pathologie / Zytologie",
        ("pathologie", "histologie", "zytologie", "biopsat", "papanicolaou", "mikroskopie"),
        ("Pathologie", "Histologie", "Zytologie", "Biopsie", "Mikroskopie"),
        0.7,
    ),
    (
        "clinical.domain.oncology_hematology",
        "Onkologie / Hämatologie",
        ("onkologie", "hamatologie", "haematologie", "tumor", "karzinom", "chemotherapie", "immuntherapie"),
        ("Onkologie", "Hämatologie", "Tumor", "Chemotherapie", "Immuntherapie"),
        0.7,
    ),
    (
        "clinical.domain.nephrology_dialysis",
        "Nephrologie / Dialyse",
        ("nephrologie", "dialyse", "hamodialyse", "haemodialyse", "peritonealdialyse", "niereninsuffizienz"),
        ("Nephrologie", "Dialyse", "Hämodialyse", "Niereninsuffizienz"),
        0.7,
    ),
    (
        "clinical.domain.prevention_vaccination",
        "Prävention / Impfen",
        ("impfung", "impfstoff", "vorsorge", "fruherkennung", "screening", "gesundheitsuntersuchung", "dmp"),
        ("Impfung", "Vorsorge", "Früherkennung", "Screening", "DMP"),
        0.68,
    ),
]

SEGMENT_DOMAIN_KIND = {
    "cardiology_report": "clinical.domain.cardiology",
    "pulmonology_report": "clinical.domain.pulmonology",
    "gastroenterology_report": "clinical.domain.gastroenterology",
    "gynecology_obstetrics": "clinical.domain.gynecology_obstetrics",
    "urology_report": "clinical.domain.urology",
    "dermatology_report": "clinical.domain.dermatology",
    "ent_report": "clinical.domain.ent",
    "neurology_report": "clinical.domain.neurology",
    "psychiatry_report": "clinical.domain.psychiatry",
    "orthopedics_report": "clinical.domain.orthopedics_trauma",
    "pediatrics_report": "clinical.domain.pediatrics",
    "surgery_report": "clinical.domain.surgery",
    "anesthesia_report": "clinical.domain.anesthesia_pain",
    "pathology_report": "clinical.domain.pathology_cytology",
    "oncology_report": "clinical.domain.oncology_hematology",
    "nephrology_report": "clinical.domain.nephrology_dialysis",
    "prevention_report": "clinical.domain.prevention_vaccination",
}

SERVICE_EVIDENCE: list[tuple[str, str, tuple[str, ...], tuple[str, ...], float]] = [
    (
        "clinical.service.consultation",
        "Beratung / Aufklärung",
        ("beratung", "aufklarung", "aufklaerung", "gesprach", "empfehlungenerlautert"),
        ("Beratung", "Aufklärung", "Gespräch", "Grundpauschale"),
        0.68,
    ),
    (
        "clinical.service.examination",
        "Klinische Untersuchung / Befund",
        ("untersuchung", "befund", "beurteilung", "status", "palpation", "inspektion"),
        ("Untersuchung", "Klinischer Befund", "Grundpauschale"),
        0.66,
    ),
    (
        "clinical.diagnostics.sonography",
        "Sonographie / Ultraschall",
        ("sonographie", "ultraschall", "sono", "echographie"),
        ("Sonographie", "Ultraschall", "Echographie"),
        0.76,
    ),
    (
        "clinical.diagnostics.doppler_sonography",
        "Dopplersonographie",
        ("dopplersonographie", "doppler", "arteriaumbilicalis"),
        ("Dopplersonographie", "Doppler", "Sonographie"),
        0.8,
    ),
    (
        "clinical.diagnostics.ctg",
        "CTG / Tokographie",
        ("ctg", "tokographie", "cardiotokographie", "ctgbeurteilung", "ctgstreifen"),
        ("CTG", "Tokographie", "Kardiotokographie"),
        0.82,
    ),
    (
        "clinical.diagnostics.prenatal_biometry",
        "Fetale Biometrie / Pränataldiagnostik",
        ("fetalebiometrie", "bpd", "femur", "fruchtwasser", "gestationsalter", "einlingsschwangerschaft"),
        ("Fetale Biometrie", "Pränataldiagnostik", "Schwangerschaft", "Sonographie"),
        0.76,
    ),
    (
        "clinical.diagnostics.endoscopy",
        "Endoskopie",
        ("endoskopie", "gastroskopie", "koloskopie", "bronchoskopie", "zystoskopie", "laryngoskopie"),
        ("Endoskopie", "Gastroskopie", "Koloskopie", "Bronchoskopie", "Zystoskopie"),
        0.76,
    ),
    (
        "clinical.diagnostics.functional_test",
        "Funktionsdiagnostik",
        ("spirometrie", "lungenfunktion", "audiometrie", "tympanometrie", "eeg", "emg", "nlg", "uroflow"),
        ("Funktionsdiagnostik", "Spirometrie", "Audiometrie", "EEG", "EMG", "Uroflow"),
        0.72,
    ),
    (
        "clinical.procedure.wound_or_minor_surgery",
        "Wundversorgung / kleiner Eingriff",
        ("wundversorgung", "wundheilungsstorung", "naht", "exzision", "abtragung", "granulationspolyp", "biopsie"),
        ("Wundversorgung", "Exzision", "Biopsie", "Operation", "Kleinchirurgie"),
        0.76,
    ),
    (
        "clinical.therapy.injection_infusion",
        "Injektion / Infusion",
        ("injektion", "infusion", "i.v.", "intravenos", "subkutan", "infiltration"),
        ("Injektion", "Infusion", "Schmerztherapie"),
        0.7,
    ),
    (
        "clinical.therapy.vaccination",
        "Impfung",
        ("impfung", "geimpft", "impfstoff", "vakzination"),
        ("Impfung", "Schutzimpfung", "Impfberatung"),
        0.72,
    ),
]

INTERNAL_SERVICE_HINTS: list[dict[str, object]] = [
    {
        "codes": ("ALL_KONGEB",),
        "kind": "internal_service.consultation_fee",
        "label": "Interner Hinweis Konsultationsgebühr",
        "text": "Interner Leistungsbogen enthält ALL_KONGEB / Konsultationsgebühr",
        "terms": ("Konsultationspauschale",),
        "candidate_gops": ("01436",),
        "confidence": 0.64,
    },
    {
        "codes": ("ALL_ORDGEB",),
        "kind": "internal_service.ordination_fee",
        "label": "Interner Hinweis Ordinationsgebühr",
        "text": "Interner Leistungsbogen enthält ALL_ORDGEB / Ordinationsgebühr",
        "terms": ("Augenärztliche Grundpauschale", "Grundpauschale Augen"),
        "candidate_gops": ("06210", "06211", "06212"),
        "confidence": 0.66,
    },
    {
        "codes": ("ALL_ORDNOT",),
        "kind": "internal_service.emergency_ordination",
        "label": "Interner Hinweis Ordinationsgebühr Notfall",
        "text": "Interner Leistungsbogen enthält ALL_ORDNOT / Ordinationsgebühr Notfall",
        "terms": ("Notfallpauschale", "Notfall", "Ordinationsgebühr", "Grundpauschale"),
        "candidate_gops": ("01210", "01212", "01214", "01216", "01218"),
        "confidence": 0.7,
    },
    {
        "codes": ("AUA_BUAHG",),
        "kind": "internal_service.ophthalmology_fundus",
        "label": "Interner Hinweis Augenhintergrund",
        "text": "Interner Leistungsbogen enthält AUA_BUAHG / binokulare Untersuchung des Augenhintergrundes",
        "terms": ("Augenhintergrund", "Fundus", "Binokulare Untersuchung des Augenhintergrundes"),
        "candidate_gops": ("06333",),
        "confidence": 0.68,
    },
    {
        "codes": ("AUA_ECHO",),
        "kind": "internal_service.aua_echo",
        "label": "Interner Hinweis Echographie Auge",
        "text": "Interner Leistungsbogen enthält AUA_ECHO / Echographie",
        "terms": ("Sonographie des Auges", "Ultraschall-Biometrie des Auges", "Ultraschall-Pachymetrie"),
        "candidate_gops": ("33000", "33001", "33002"),
        "confidence": 0.62,
    },
    {
        "codes": ("AUA_EPU", "ERG", "VEP"),
        "kind": "internal_service.aua_epu",
        "label": "Interner Hinweis elektrophysiologische Untersuchung",
        "text": "Interner Leistungsbogen enthält elektrophysiologische Augen-Diagnostik",
        "terms": ("Elektrophysiologische Untersuchung", "Elektroretinographie", "VEP"),
        "candidate_gops": ("06312",),
        "confidence": 0.62,
    },
    {
        "codes": ("AUA_FAG",),
        "kind": "internal_service.aua_fag",
        "label": "Interner Hinweis Fluoreszenzangiographie",
        "text": "Interner Leistungsbogen enthält AUA_FAG / Fluoreszenzangiographie",
        "terms": ("Fluoreszenzangiographie", "Angiographie Auge"),
        "candidate_gops": ("06331",),
        "confidence": 0.62,
    },
    {
        "codes": ("AUA_PDT",),
        "kind": "internal_service.aua_pdt",
        "label": "Interner Hinweis PDT",
        "text": "Interner Leistungsbogen enthält AUA_PDT / PDT",
        "terms": ("PDT", "Photodynamische Therapie"),
        "candidate_gops": ("06332",),
        "confidence": 0.62,
    },
    {
        "codes": ("AUA_PERI",),
        "kind": "internal_service.aua_peri",
        "label": "Interner Hinweis Perimetrie",
        "text": "Interner Leistungsbogen enthält AUA_PERI / Perimetrie",
        "terms": ("Perimetrie", "Gesichtsfeld"),
        "candidate_gops": ("06330",),
        "confidence": 0.62,
    },
    {
        "codes": ("AUA_SCHIEL",),
        "kind": "internal_service.aua_schiel",
        "label": "Interner Hinweis Schielbehandlung",
        "text": "Interner Leistungsbogen enthält AUA_SCHIEL / quantitative Untersuchung des binokularen Sehens",
        "terms": ("Schielbehandlung", "Quantitative Untersuchung des binokularen Sehens"),
        "candidate_gops": ("06320", "06321"),
        "confidence": 0.62,
    },
    {
        "codes": ("TWS",),
        "kind": "internal_service.aua_tws",
        "label": "Interner Hinweis Tränenweg-Sondierung",
        "text": "Interner Leistungsbogen enthält TWS / TW-Sondierung",
        "terms": ("Tränenweg", "Tränenwege", "Sondierung", "Kleinchirurgischer Eingriff am Auge"),
        "candidate_gops": ("06352",),
        "confidence": 0.58,
    },
    {
        "codes": ("AUA_LIDHEB",),
        "kind": "internal_service.aua_lidheber",
        "label": "Interner Hinweis Lidheber-Operation",
        "text": "Interner Leistungsbogen enthält AUA_LIDHEB / OP der Lidsenkung mit Lidheber",
        "terms": ("Lidheber", "Lidoperation", "Kleinchirurgischer Eingriff am Auge"),
        "candidate_gops": ("06350", "06351", "06352"),
        "confidence": 0.58,
    },
]


def _compact(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", "", normalized.lower())


def _date_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    day, month, year = value.split(".")
    return f"{year}-{month}-{day}"


def _first_date(text: str) -> str | None:
    match = DATE_RE.search(text)
    return _date_to_iso(match.group(1)) if match else None


def _first_time(text: str) -> str | None:
    match = TIME_RE.search(text)
    return match.group(1) if match else None


def _first_service_date(text: str) -> str | None:
    for match in DATE_RE.finditer(text):
        before = text[max(0, match.start() - 60) : match.start()]
        before_key = re.sub(r"[^a-z0-9]+", "", _compact(before))
        if before_key.endswith(("geb", "gebdat", "geburtsdatum", "geboren", "gebam")):
            continue
        if any(token in before_key[-30:] for token in ("gebdat", "geburtsdatum", "geboren", "gebam")):
            continue
        return _date_to_iso(match.group(1))
    return None


def _service_datetime(text: str, fallback: bool = True) -> tuple[str | None, str | None]:
    compact = _compact(text)
    datetime_patterns = [
        r"durchgef.{0,40}?(\d{2}\.\d{2}\.\d{4})um(\d{2}:\d{2})",
        r"probenentnahmedat\.?(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"bezugsdatum(\d{2}\.\d{2}\.\d{4})zeit(\d{2}:\d{2})",
        r"aufnahmezna(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"aufnahme(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"termindgf:?(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"leistungam(\d{2}\.\d{2}\.\d{4})um(\d{2}:\d{2})",
        r"befundetam(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"auftragsdatum(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"(\d{2}\.\d{2}\.\d{4})\(?start:?(\d{2}:\d{2})",
        r"ctgstreifenvom(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
    ]
    for pattern in datetime_patterns:
        match = re.search(pattern, compact)
        if match:
            return _date_to_iso(match.group(1)), match.group(2)

    date_patterns = [
        r"am(\d{2}\.\d{2}\.\d{4})inambulanterbehandlung",
        r"datum:?(\d{2}\.\d{2}\.\d{4})",
        r"ctgstreifenvom(\d{2}\.\d{2}\.\d{4})",
        r"behandlungsdatum:?(\d{2}\.\d{2}\.\d{4})",
    ]
    for pattern in date_patterns:
        match = re.search(pattern, compact)
        if match:
            return _date_to_iso(match.group(1)), _first_time(text)

    if not fallback:
        return None, None
    return _first_service_date(text), _first_time(text)


def _treatment_end_datetime(text: str) -> tuple[str | None, str | None]:
    compact = _compact(text)
    patterns = [
        r"ended\.?behand(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"endebehand(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return _date_to_iso(match.group(1)), match.group(2)
    return None, None


def _extract_icd10(text: str) -> str | None:
    compact_upper = _compact(text).upper()
    for marker in ("DIAGNOSE", "DIAGNOSEN"):
        index = compact_upper.find(marker)
        if index >= 0:
            match = ICD_RE.search(compact_upper[index : index + 500])
            if match:
                return match.group(1)
    match = ICD_RE.search(compact_upper)
    return match.group(1) if match else None


def _evidence_id(kind: str, page: int, text: str) -> str:
    digest = hashlib.sha1(f"{kind}:{page}:{text}".encode("utf-8")).hexdigest()[:10]
    return f"ev-{digest}"


def extract_evidence(
    pages: list[PageText],
    segments: list[DocumentSegment],
) -> tuple[list[Evidence], list[ReviewCandidate], list[ExcludedEvidence], dict[str, str | None]]:
    relevant_pages = set()
    segment_type_by_page: dict[int, str] = {}
    for segment in segments:
        for page_no in range(segment.start_page, segment.end_page + 1):
            segment_type_by_page[page_no] = segment.segment_type
            if segment.relevant_for_billing:
                relevant_pages.add(page_no)

    evidence: list[Evidence] = []
    review: list[ReviewCandidate] = []
    excluded: list[ExcludedEvidence] = []
    case_context: dict[str, str | None] = {
        "treatment_start": None,
        "treatment_end": None,
        "quarter": None,
        "diagnosis": None,
    }
    last_lab_datetime: tuple[str | None, str | None] = (None, None)

    for page in pages:
        segment_type = segment_type_by_page.get(page.page, "other")
        text = page.text or ""
        compact = _compact(text)
        if segment_type == "laboratory_result":
            explicit_lab_datetime = _service_datetime(text, fallback=False)
            if explicit_lab_datetime[0]:
                last_lab_datetime = explicit_lab_datetime

        if segment_type in CLINICAL_CONTEXT_SEGMENTS:
            if ("kv-abrechnung" in compact and "notfall" in compact) or "aufnahmezna" in compact:
                service_date, service_time = _service_datetime(text)
                case_context["treatment_start"] = case_context["treatment_start"] or _join_datetime(service_date, service_time)
                evidence.append(
                    _ev(
                        "context.kv_notfall_zna",
                        "KV-Notfall/ZNA",
                        page.page,
                        text,
                        service_date,
                        service_time,
                        0.95,
                    )
                )

            specialty_evidence = _extract_specialty_ambulance(page, segment_type)
            evidence.extend(specialty_evidence)
            for item in specialty_evidence:
                if item.kind == "context.specialty_ambulance_emergency" and item.service_date:
                    case_context["treatment_start"] = case_context["treatment_start"] or _join_datetime(item.service_date, item.service_time)

            end_date, end_time = _treatment_end_datetime(text)
            if end_date:
                case_context["treatment_end"] = _join_datetime(end_date, end_time)

            diagnosis = _extract_icd10(text)
            if diagnosis:
                diagnosis_date, diagnosis_time = _service_datetime(text)
                case_context["diagnosis"] = diagnosis
                evidence.append(
                    _ev(
                        "diagnosis.icd10",
                        f"ICD-10 {diagnosis}",
                        page.page,
                        diagnosis,
                        diagnosis_date,
                        diagnosis_time,
                        0.75,
                    )
                )

        if page.page in relevant_pages:
            generic_evidence = _extract_generic_clinical_evidence(page, segment_type)
            evidence.extend(generic_evidence)
            for item in generic_evidence:
                if item.service_date:
                    case_context["treatment_start"] = case_context["treatment_start"] or _join_datetime(item.service_date, item.service_time)
            evidence.extend(_extract_radiology(page, segment_type))
            evidence.extend(_extract_labs(page, segment_type, last_lab_datetime))
            evidence.extend(_extract_ecg(page, segment_type))

        evidence.extend(_extract_internal_service_hints(page, segment_type))

        if segment_type in CLINICAL_CONTEXT_SEGMENTS or segment_type in {"consult", "data_capture"}:
            review.extend(_extract_review_candidates(page, segment_type))
            excluded.extend(_extract_exclusions(page, segment_type))

    if case_context["treatment_start"]:
        case_context["quarter"] = quarter_from_date(case_context["treatment_start"][:10])

    return _dedupe_evidence(evidence), _dedupe_review(review), _dedupe_excluded(excluded), case_context


def _join_datetime(date_value: str | None, time_value: str | None) -> str | None:
    if not date_value:
        return None
    return f"{date_value}T{time_value or '00:00'}:00"


def quarter_from_date(date_value: str | None) -> str | None:
    if not date_value:
        return None
    year, month, _ = date_value.split("-")
    month_number = int(month)
    quarter = (month_number - 1) // 3 + 1
    return f"{year}/Q{quarter}"


def _ev(
    kind: str,
    label: str,
    page: int,
    text: str,
    service_date: str | None,
    service_time: str | None,
    confidence: float,
    value: str | None = None,
    unit: str | None = None,
    metadata: dict[str, object] | None = None,
) -> Evidence:
    snippet = re.sub(r"\s+", " ", text).strip()[:240]
    return Evidence(
        evidence_id=_evidence_id(kind, page, snippet),
        kind=kind,
        label=label,
        page=page,
        service_date=service_date,
        service_time=service_time,
        value=value,
        unit=unit,
        text=snippet,
        confidence=confidence,
        metadata=metadata or {},
    )


def _search_terms(*terms: str) -> dict[str, object]:
    return {"search_terms": [term for term in terms if term]}


def _candidate_metadata(terms: tuple[str, ...], candidate_gops: tuple[str, ...]) -> dict[str, object]:
    metadata = _search_terms(*terms)
    if candidate_gops:
        metadata["candidate_gops"] = list(candidate_gops)
    return metadata


def _has_internal_code(text: str, *codes: str) -> bool:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    for code in codes:
        pattern = rf"(?<![a-z0-9_])(?:\d+(?:[,.]\d+)?\s*)?{re.escape(code.lower())}(?![a-z0-9_])"
        if re.search(pattern, folded):
            return True
    return False


def _matches_markers(kind: str, compact: str, markers: tuple[str, ...]) -> bool:
    if kind == "clinical.domain.surgery" and any(marker in compact for marker in ("geplantereingriff", "opplanung", "kanngeplantwerden")):
        if not any(marker in compact for marker in ("durchgefuhrt", "opbericht", "operationsbericht")):
            return False
    if kind == "clinical.domain.dermatology":
        clear_dermatology = tuple(marker for marker in markers if marker != "haut")
        if any(marker in compact for marker in clear_dermatology):
            return True
        return "haut" in compact and "netzhaut" not in compact and "netzahut" not in compact
    if kind == "clinical.domain.urology":
        clear_urology = ("prostata", "harnblase", "zystoskopie", "cystoskopie", "uroflow", "harnstau", "ureter", "urin")
        if "neurologie" in compact and not any(marker in compact for marker in clear_urology):
            return False
        if any(marker in compact for marker in ("nephrologie", "dialyse", "niereninsuffizienz")) and not any(marker in compact for marker in clear_urology):
            return False
    return any(marker in compact for marker in markers)


def _extract_generic_clinical_evidence(page: PageText, segment_type: str) -> list[Evidence]:
    if segment_type not in CLINICAL_CONTEXT_SEGMENTS:
        return []

    text = page.text
    compact = _compact(text)
    service_date, service_time = _service_datetime(text)
    found: list[Evidence] = []
    primary_domain_kind = SEGMENT_DOMAIN_KIND.get(segment_type)
    broad_domain_matching = segment_type in {"case_context", "treatment_report", "clinical_report"}

    for kind, label, markers, terms, confidence in DOMAIN_EVIDENCE:
        is_primary_domain = kind == primary_domain_kind
        is_emergency_domain = kind == "clinical.domain.emergency" and _matches_markers(kind, compact, markers)
        is_broad_domain_match = broad_domain_matching and _matches_markers(kind, compact, markers)
        if is_primary_domain or is_emergency_domain or is_broad_domain_match:
            found.append(
                _ev(
                    kind,
                    label,
                    page.page,
                    text,
                    service_date,
                    service_time,
                    confidence,
                    metadata=_search_terms(*terms),
                )
            )

    for kind, label, markers, terms, confidence in SERVICE_EVIDENCE:
        if any(marker in compact for marker in markers):
            found.append(
                _ev(
                    kind,
                    label,
                    page.page,
                    text,
                    service_date,
                    service_time,
                    confidence,
                    metadata=_search_terms(*terms),
                )
            )

    if not found and segment_type not in {"laboratory_result", "radiology_report", "ecg", "ctg"}:
        found.append(
            _ev(
                "clinical.document.general",
                "Klinischer Dokumentinhalt",
                page.page,
                text,
                service_date,
                service_time,
                0.58,
                metadata=_search_terms("Grundpauschale", "Untersuchung", "Beratung"),
            )
        )

    return found


def _extract_specialty_ambulance(page: PageText, segment_type: str) -> list[Evidence]:
    if segment_type not in CLINICAL_CONTEXT_SEGMENTS:
        return []

    text = page.text
    compact = _compact(text)
    service_date, service_time = _service_datetime(text)
    found: list[Evidence] = []

    if (
        "notfallambulanz" in compact
        or "notfallambulanzaugenklinik" in compact
        or "notfallmassig" in compact
        or "notfallsymptomorientierteuntersuchung" in compact
        or "notfall-symptomorientierteuntersuchung" in compact
    ):
        search_terms = [
            "Notfallpauschale",
            "Notfall",
            "Grundpauschale",
        ]
        if "augen" in compact:
            search_terms.extend(["Augenheilkunde", "Augenärztliche Grundpauschale"])
        if any(token in compact for token in ("frauenklinik", "gynakologie", "geburtshilfe", "schwangerschaft")):
            search_terms.extend(["Gynäkologie", "Geburtshilfe", "Schwangerschaft"])

        found.append(
            _ev(
                "context.specialty_ambulance_emergency",
                "Fachambulanz-/Notfallkontakt",
                page.page,
                "Notfallkontakt in einer Fachambulanz dokumentiert",
                service_date,
                service_time,
                0.88,
                metadata=_search_terms(*search_terms),
            )
        )

    if "ambulanzaugen-befund" in compact or "augenambulanz" in compact or "augenklinik" in compact:
        found.append(
            _ev(
                "clinical.ophthalmology_report",
                "Augenärztlicher Ambulanzbefund",
                page.page,
                text,
                service_date,
                service_time,
                0.84,
                metadata=_search_terms(
                    "Augenärztliche Untersuchung",
                    "Augenheilkunde",
                    "Ophthalmologische Untersuchung",
                    "Grundpauschale Augen",
                ),
            )
        )

    if any(
        token in compact
        for token in [
            "visus",
            "tensio",
            "vordereraugenabschnitt",
            "hintereraugenabschnitt",
            "hornhaut-topographie",
            "fluoreszein",
            "schirmer-test",
        ]
    ):
        found.append(
            _ev(
                "clinical.ophthalmology_exam",
                "Ophthalmologische Untersuchung",
                page.page,
                text,
                service_date,
                service_time,
                0.86,
                metadata=_search_terms(
                    "Augenärztliche Untersuchung",
                    "Visus",
                    "Tonometrie",
                    "Spaltlampenuntersuchung",
                    "Hornhaut",
                ),
            )
        )

    if "hintereraugenabschnitt" in compact or "netzhautzentralanliegend" in compact:
        found.append(
            _ev(
                "clinical.ophthalmology_fundus",
                "Augenhintergrund / Fundus-Hinweis",
                page.page,
                text,
                service_date,
                service_time,
                0.78,
                metadata=_search_terms(
                    "Augenhintergrund",
                    "Fundus",
                    "Binokulare Untersuchung des Augenhintergrundes",
                ),
            )
        )

    return found


def _extract_ecg(page: PageText, segment_type: str) -> list[Evidence]:
    if segment_type != "ecg":
        return []

    text = page.text
    compact = _compact(text)
    service_date, service_time = _service_datetime(text)
    found: list[Evidence] = []

    if (
        "standard12ableitungen" in compact
        or "12-kanal-ekg" in compact
        or "12kanalekg" in compact
        or "ekg" in compact
        or "sinusrhythmus" in compact
    ):
        found.append(
            _ev(
                "clinical.ecg_12_lead",
                "12-Kanal-EKG",
                page.page,
                text,
                service_date,
                service_time,
                0.84,
                metadata=_search_terms(
                    "EKG",
                    "12-Kanal-EKG",
                    "Elektrokardiogramm",
                    "Standard 12 Ableitungen",
                    "Ruhe-EKG",
                ),
            )
        )

    if "sinusrhythmus" in compact:
        found.append(
            _ev(
                "clinical.ecg_rhythm_findings",
                "EKG-Rhythmusbefund",
                page.page,
                "Sinusrhythmus im EKG dokumentiert",
                service_date,
                service_time,
                0.72,
                metadata=_search_terms("EKG", "Rhythmusbefund", "Sinusrhythmus"),
            )
        )

    return found


def _extract_internal_service_hints(page: PageText, segment_type: str) -> list[Evidence]:
    if segment_type != "data_capture":
        return []

    text = page.text
    service_date, service_time = _service_datetime(text)
    found: list[Evidence] = []

    for hint in INTERNAL_SERVICE_HINTS:
        codes = tuple(str(code) for code in hint["codes"])
        if _has_internal_code(text, *codes):
            found.append(
                _ev(
                    str(hint["kind"]),
                    str(hint["label"]),
                    page.page,
                    str(hint["text"]),
                    service_date,
                    service_time,
                    float(hint["confidence"]),
                    metadata=_candidate_metadata(
                        tuple(str(term) for term in hint["terms"]),
                        tuple(str(gop) for gop in hint["candidate_gops"]),
                    ),
                )
            )

    return found


def _extract_radiology(page: PageText, segment_type: str) -> list[Evidence]:
    if segment_type not in {"radiology_report", "treatment_report"}:
        return []

    text = page.text
    compact = _compact(text)
    service_date, service_time = _service_datetime(text, fallback=False)
    found: list[Evidence] = []

    if ("ctkopfnativ" in compact or "ctctschadelnativ" in compact) and "durchgefuhrt" in compact:
        found.append(_ev("radiology.ct_head_native", "CT Kopf nativ", page.page, "CT Kopf nativ durchgeführt", service_date, service_time, 0.96))

    if ("rontgenschulter2eb" in compact or "roeschulter2eb" in compact) and "durchgefuhrt" in compact:
        found.append(_ev("radiology.xray_shoulder_2_planes", "Röntgen Schulter 2 Ebenen", page.page, "Röntgen Schulter 2 Ebenen durchgeführt", service_date, service_time, 0.96))

    if ("rontgenhws2ebenen" in compact or "roehws2ebenen" in compact) and "durchgefuhrt" in compact:
        found.append(_ev("radiology.xray_spine_hws_2_planes", "Röntgen HWS 2 Ebenen", page.page, "Röntgen HWS 2 Ebenen durchgeführt", service_date, service_time, 0.96))

    if ("rontgenlunge" in compact or "roelunge" in compact or "thorax" in compact) and ("2ebenen" in compact or "p.a." in text.lower()):
        found.append(_ev("radiology.xray_thorax_2_planes", "Röntgen Thorax/Lunge 2 Ebenen", page.page, "Röntgen Thorax/Lunge 2 Ebenen", service_date, service_time, 0.86))

    if ("ctlws" in compact or "ct-lws" in compact or "ctcthws" in compact or "cthws" in compact) and "durchgefuhrt" in compact and "storniert" not in compact:
        found.append(_ev("radiology.ct_spine_section", "CT Wirbelsäulenabschnitt", page.page, "CT Wirbelsäulenabschnitt durchgeführt", service_date, service_time, 0.84))

    if ("+km" in compact or "kontrastmittel" in compact or "imeron" in compact) and "nativ" not in compact:
        found.append(_ev("radiology.ct_contrast", "CT-Kontrastmittel", page.page, "Kontrastmittelgabe dokumentiert", service_date, service_time, 0.8))

    return found


def _extract_labs(
    page: PageText,
    segment_type: str,
    carried_datetime: tuple[str | None, str | None],
) -> list[Evidence]:
    if segment_type != "laboratory_result":
        return []

    text = page.text
    compact = _compact(text)
    service_date, service_time = _service_datetime(text, fallback=False)
    if not service_date:
        service_date, service_time = carried_datetime
    found: list[Evidence] = []

    lab_patterns = [
        ("lab.creatinine", "Kreatinin", "kreatinin"),
        ("lab.sodium", "Natrium", "natrium"),
        ("lab.potassium", "Kalium", "kalium"),
        ("lab.glucose", "Glucose", "glucose"),
        ("lab.alt_gpt", "ALT/GPT", "alt"),
        ("lab.erythrocytes", "Erythrozyten", "erythrozyten"),
        ("lab.leukocytes", "Leukozyten", "leukozyten"),
        ("lab.thrombocytes", "Thrombozyten", "thrombozyten"),
        ("lab.hemoglobin", "Hämoglobin", "hamoglobin"),
        ("lab.hematocrit", "Hämatokrit", "hamatokrit"),
    ]

    if "quick" in compact and "probeunterfullt" not in compact:
        found.append(_ev("lab.quick", "Quick", page.page, "Quick-Wert valide", service_date, service_time, 0.9))

    for kind, label, needle in lab_patterns:
        if needle in compact:
            found.append(_ev(kind, label, page.page, label, service_date, service_time, 0.86))

    return found


def _extract_review_candidates(page: PageText, segment_type: str) -> list[ReviewCandidate]:
    text = page.text
    compact = _compact(text)
    candidates: list[ReviewCandidate] = []

    if segment_type == "consult" and "neurologie" in text.lower():
        candidates.append(ReviewCandidate(evidence="Neurologisches Konsil", evidence_pages=[page.page], reason="Interne Konsiltypen sind nicht automatisch EBM-GOPs."))
    if segment_type == "consult" and ("psych" in text.lower() or "psychische" in text.lower()):
        candidates.append(ReviewCandidate(evidence="Psychiatrisches Konsil", evidence_pages=[page.page], reason="Interne Konsiltypen sind nicht automatisch EBM-GOPs."))
    if "schwangerschaftstest" in compact or "schwangerschaftsnachweis" in compact:
        candidates.append(ReviewCandidate(evidence="Schwangerschaftstest Urin", evidence_pages=[page.page], possible_gops=["32132"], reason="Katalogtreffer möglich, aber noch keine validierte Positivregel."))
    if "drogen" in compact and "urin" in compact:
        candidates.append(ReviewCandidate(evidence="Drogen-Screening Urin", evidence_pages=[page.page], possible_gops=["32292", "32307"], reason="Panel-/Einzeltestlogik und Abrechnungsfähigkeit nicht validiert."))
    if "urinstatus" in compact:
        candidates.append(ReviewCandidate(evidence="Urinstatus", evidence_pages=[page.page], possible_gops=["32720"], reason="Im Goldstandard noch keine Positivregel."))
    if any(token in compact for token in ["crp", "ck-mb", "myoglobin", "harnstoff", "gamma-gt", "ast"]):
        candidates.append(ReviewCandidate(evidence="Erweiterte Laborwerte", evidence_pages=[page.page], possible_gops=["32065", "32069", "32071", "32074", "32092", "32128", "32450"], reason="Nicht jeder dokumentierte Laborwert wird automatisch abgerechnet."))
    if segment_type == "data_capture":
        for hint in INTERNAL_SERVICE_HINTS:
            codes = tuple(str(code) for code in hint["codes"])
            if _has_internal_code(text, *codes):
                candidates.append(
                    ReviewCandidate(
                        evidence=str(hint["label"]),
                        evidence_pages=[page.page],
                        possible_gops=[str(gop) for gop in hint["candidate_gops"]],
                        reason="Interner Leistungsbogenhinweis; klinische Evidenz und Abrechnungsfähigkeit prüfen.",
                    )
                )

    return candidates


def _extract_exclusions(page: PageText, segment_type: str) -> list[ExcludedEvidence]:
    text = page.text
    compact = _compact(text)
    excluded: list[ExcludedEvidence] = []

    if "ctcthws" in compact and "storniert" in compact:
        excluded.append(ExcludedEvidence(evidence="CT HWS nativ", evidence_pages=[page.page], not_billed_gop="34311", reason="Nur storniert dokumentiert; kein durchgeführter Befund."))
    if "ctkopfnativ" in compact and "nativ" in compact:
        excluded.append(ExcludedEvidence(evidence="CT-Kontrastmittelzuschlag", evidence_pages=[page.page], not_billed_gop="34345", reason="CT als nativ dokumentiert; keine Kontrastmittelgabe."))
    if "probeunterfullt" in compact:
        excluded.append(ExcludedEvidence(evidence="Gerinnungsprobe", evidence_pages=[page.page], reason="Probe unterfüllt/falsches Mischungsverhältnis."))
    if "ras9048" in compact:
        excluded.append(ExcludedEvidence(evidence="Interner Radiologie-Zuschlag RAS9048", evidence_pages=[page.page], reason="Lokaler interner Code ohne freigegebenes EBM-/Hessen-GOP-Mapping."))

    return excluded


def _dedupe_evidence(items: list[Evidence]) -> list[Evidence]:
    seen: set[tuple[str, int]] = set()
    result: list[Evidence] = []
    for item in items:
        key = (item.kind, item.page)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _dedupe_review(items: list[ReviewCandidate]) -> list[ReviewCandidate]:
    merged: dict[tuple[str, str], ReviewCandidate] = {}
    for item in items:
        key = (item.evidence, item.reason)
        if key not in merged:
            merged[key] = item
        else:
            merged[key].evidence_pages = sorted(set(merged[key].evidence_pages + item.evidence_pages))
    return list(merged.values())


def _dedupe_excluded(items: list[ExcludedEvidence]) -> list[ExcludedEvidence]:
    merged: dict[tuple[str, str], ExcludedEvidence] = {}
    for item in items:
        key = (item.evidence, item.reason)
        if key not in merged:
            merged[key] = item
        else:
            merged[key].evidence_pages = sorted(set(merged[key].evidence_pages + item.evidence_pages))
    return list(merged.values())
