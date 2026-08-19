from __future__ import annotations

import re
import unicodedata

from .models import DocumentSegment, PageText


SEGMENT_LABELS = {
    "case_context": "Fallkontext / ZNA",
    "treatment_report": "Behandlungsbericht",
    "radiology_report": "Radiologiebefund",
    "laboratory_result": "Laborbefund",
    "consult": "Konsil",
    "ecg": "EKG",
    "ctg": "CTG / Tokographie",
    "clinical_report": "Klinischer Bericht",
    "cardiology_report": "Kardiologie",
    "pulmonology_report": "Pneumologie",
    "gastroenterology_report": "Gastroenterologie / Endoskopie",
    "gynecology_obstetrics": "Gynaekologie / Geburtshilfe",
    "urology_report": "Urologie",
    "dermatology_report": "Dermatologie",
    "ent_report": "HNO",
    "neurology_report": "Neurologie",
    "psychiatry_report": "Psychiatrie / Psychotherapie",
    "orthopedics_report": "Orthopaedie / Unfallchirurgie",
    "pediatrics_report": "Paediatrie",
    "surgery_report": "Chirurgie / OP",
    "anesthesia_report": "Anaesthesie / Schmerztherapie",
    "pathology_report": "Pathologie / Zytologie",
    "oncology_report": "Onkologie / Haematologie",
    "nephrology_report": "Nephrologie / Dialyse",
    "prevention_report": "Praevention / Impfen",
    "therapy_report": "Therapie / Heilmittel",
    "data_capture": "Datenerfassung",
    "request": "Anforderung / Indikationspruefung",
    "other": "Sonstiges Dokument",
}

CLINICAL_TYPES = {
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

RELEVANT_TYPES = CLINICAL_TYPES

DOMAIN_MARKERS: list[tuple[str, str, tuple[str, ...]]] = [
    (
        "gynecology_obstetrics",
        "Gynaekologie-/Geburtshilfe-Marker gefunden",
        (
            "gynakologie",
            "geburtshilfe",
            "frauenklinik",
            "notfallambulanzfrauenklinik",
            "pranatal",
            "praenatal",
            "schwangerschaft",
            "ssw",
            "fetale",
            "fetal",
            "ctg",
            "tokographie",
            "dopplersonographie",
            "vaginal",
            "zervix",
            "uterus",
            "ovar",
            "mamma",
            "granulationspolyp",
            "dammriss",
        ),
    ),
    (
        "cardiology_report",
        "Kardiologie-Marker gefunden",
        (
            "kardiologie",
            "herz",
            "koronar",
            "angina",
            "herzinsuffizienz",
            "echokardiographie",
            "herzkatheter",
            "schrittmacher",
            "defibrillator",
            "langzeit-ekg",
            "belastungs-ekg",
        ),
    ),
    (
        "pulmonology_report",
        "Pneumologie-Marker gefunden",
        (
            "pneumologie",
            "lunge",
            "bronchoskopie",
            "spirometrie",
            "lungenfunktion",
            "asthma",
            "copd",
            "sauerstoff",
            "schlafapnoe",
        ),
    ),
    (
        "gastroenterology_report",
        "Gastroenterologie-/Endoskopie-Marker gefunden",
        (
            "gastroenterologie",
            "gastroskopie",
            "koloskopie",
            "coloskopie",
            "rektoskopie",
            "endoskopie",
            "abdomen",
            "leber",
            "galle",
            "pankreas",
            "hepatitis",
        ),
    ),
    (
        "urology_report",
        "Urologie-Marker gefunden",
        (
            "urologie",
            "prostata",
            "harnblase",
            "zystoskopie",
            "cystoskopie",
            "uroflow",
            "harnstau",
            "niere",
            "ureter",
            "urin",
        ),
    ),
    (
        "dermatology_report",
        "Dermatologie-Marker gefunden",
        (
            "dermatologie",
            "haut",
            "naevus",
            "navi",
            "melanom",
            "ekzem",
            "psoriasis",
            "dermatoskopie",
            "exzision",
        ),
    ),
    (
        "ent_report",
        "HNO-Marker gefunden",
        (
            "hno",
            "hals-nasen-ohren",
            "audiometrie",
            "tympanometrie",
            "laryngoskopie",
            "rhinologie",
            "tonsillen",
            "ohr",
            "nase",
            "kehlkopf",
        ),
    ),
    (
        "neurology_report",
        "Neurologie-Marker gefunden",
        (
            "neurologie",
            "eeg",
            "emg",
            "nlg",
            "epilepsie",
            "schlaganfall",
            "parese",
            "parkinson",
            "demenz",
        ),
    ),
    (
        "psychiatry_report",
        "Psychiatrie-/Psychotherapie-Marker gefunden",
        (
            "psychiatrie",
            "psychotherapie",
            "psychosomatik",
            "depression",
            "angststorung",
            "suizid",
            "sucht",
            "gesprachstherapie",
        ),
    ),
    (
        "orthopedics_report",
        "Orthopaedie-/Unfallchirurgie-Marker gefunden",
        (
            "orthopadie",
            "orthopaedie",
            "unfallchirurgie",
            "fraktur",
            "luxation",
            "gelenk",
            "wirbelsaule",
            "knie",
            "schulter",
            "trauma",
        ),
    ),
    (
        "pediatrics_report",
        "Paediatrie-Marker gefunden",
        (
            "paediatrie",
            "padiatrie",
            "kinderklinik",
            "jugendmedizin",
            "u-untersuchung",
            "saugling",
            "kind",
            "neugeboren",
        ),
    ),
    (
        "surgery_report",
        "Chirurgie-/OP-Marker gefunden",
        (
            "chirurgie",
            "op-bericht",
            "operationsbericht",
            "operation",
            "eingriff",
            "wundversorgung",
            "naht",
            "biopsie",
            "exzision",
            "abtragung",
        ),
    ),
    (
        "anesthesia_report",
        "Anaesthesie-/Schmerztherapie-Marker gefunden",
        (
            "anasthesie",
            "anaesthesie",
            "narkose",
            "schmerztherapie",
            "palliativ",
            "regionalanasthesie",
            "sedierung",
        ),
    ),
    (
        "pathology_report",
        "Pathologie-/Zytologie-Marker gefunden",
        (
            "pathologie",
            "histologie",
            "zytologie",
            "zytologisch",
            "biopsat",
            "papanicolaou",
            "mikroskopie",
        ),
    ),
    (
        "oncology_report",
        "Onkologie-/Haematologie-Marker gefunden",
        (
            "onkologie",
            "hamatologie",
            "haematologie",
            "tumor",
            "karzinom",
            "chemotherapie",
            "immuntherapie",
            "strahlentherapie",
        ),
    ),
    (
        "nephrology_report",
        "Nephrologie-/Dialyse-Marker gefunden",
        (
            "nephrologie",
            "dialyse",
            "hamodialyse",
            "haemodialyse",
            "peritonealdialyse",
            "niereninsuffizienz",
        ),
    ),
    (
        "prevention_report",
        "Praeventions-/Impfmarker gefunden",
        (
            "impfung",
            "impfstoff",
            "vorsorge",
            "fruherkennung",
            "screening",
            "gesundheitsuntersuchung",
            "dmp",
        ),
    ),
    (
        "therapy_report",
        "Therapie-/Heilmittel-Marker gefunden",
        (
            "physiotherapie",
            "ergotherapie",
            "logopadie",
            "heilmittel",
            "injektion",
            "infusion",
            "verband",
        ),
    ),
]


def _compact(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"\s+", "", normalized.lower())


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()


def _matches_domain(segment_type: str, compact: str, markers: tuple[str, ...]) -> bool:
    if segment_type == "urology_report":
        clear_urology = ("prostata", "harnblase", "zystoskopie", "cystoskopie", "uroflow", "harnstau", "ureter", "urin")
        if "neurologie" in compact and not any(marker in compact for marker in clear_urology):
            return False
        if any(marker in compact for marker in ("nephrologie", "dialyse", "niereninsuffizienz")) and not any(marker in compact for marker in clear_urology):
            return False
    return any(marker in compact for marker in markers)


def classify_page(text: str) -> tuple[str, float, list[str]]:
    lower = _fold(text)
    compact = _compact(text)
    reasons: list[str] = []

    if "laborbefund" in lower or ("untersuchung" in lower and "referenzbereich" in lower):
        reasons.append("Laborbefund-Marker gefunden")
        return "laboratory_result", 0.95, reasons
    if "radiologie - befund" in lower or ("befund" in lower and "ctkopf" in compact):
        reasons.append("Radiologiebefund-Marker gefunden")
        return "radiology_report", 0.95, reasons
    if (
        "ambulanzaugen-anforderung" in compact
        or "status:angefordert" in compact
        or ("anforderung" in lower and "auftragsdatum" in lower)
        or "indikationspr" in lower
        or "angeforderte untersuchungen" in lower
    ):
        reasons.append("Anforderungs-/Indikationsmarker gefunden")
        return "request", 0.72, reasons
    if (
        "ambulanzaugen-befund" in compact
        or "notfall-symptomorientierteuntersuchung" in compact
        or ("notfallambulanzaugenklinik" in compact and "befund" in lower)
        or ("augenambulanz" in compact and ("anamnese" in lower or "beurteilung" in lower))
        or "vordereraugenabschnitt" in compact
        or "hintereraugenabschnitt" in compact
        or ("beurteilung:" in lower and "therapie" in lower)
        or ("diagnose:" in lower and "herpeskeratitis" in compact)
        or "wirberichtenihnen" in compact and "augenambulanz" in compact
    ):
        reasons.append("Augenambulanz-/Fachambulanz-Befund gefunden")
        return "treatment_report", 0.86, reasons
    if "behandlungsbericht zna" in lower or "diagnostik:" in lower and "aufnahme" in lower:
        reasons.append("Behandlungsbericht/ZNA-Marker gefunden")
        return "treatment_report", 0.88, reasons
    if "aufnahmezna" in compact or "kv-abrechnung/notfalldienst" in compact:
        reasons.append("ZNA-/KV-Notfall-Kontext gefunden")
        return "case_context", 0.9, reasons
    if "konsil - befund" in lower or "konsil" in lower and "durchgef" in lower:
        reasons.append("Konsil-Marker gefunden")
        return "consult", 0.82, reasons
    if "standard 12 ableitungen" in lower or "ekg" in lower or "sinusrhythmus" in lower:
        reasons.append("EKG-Marker gefunden")
        return "ecg", 0.78, reasons
    if "ctg" in compact or "tokographie" in compact or "cardiotokographie" in compact:
        reasons.append("CTG-/Tokographie-Marker gefunden")
        return "ctg", 0.82, reasons

    for segment_type, reason, markers in DOMAIN_MARKERS:
        if _matches_domain(segment_type, compact, markers):
            reasons.append(reason)
            return segment_type, 0.82, reasons

    if (
        "wirberichten" in compact
        or "behandlungsbericht" in compact
        or "arztbrief" in compact
        or ("anamnese" in compact and any(marker in compact for marker in ("befund", "beurteilung", "procedere", "therapie", "diagnose")))
        or ("verlauf" in compact and any(marker in compact for marker in ("kontrolle", "befund", "beurteilung")))
    ):
        reasons.append("Allgemeiner klinischer Bericht gefunden")
        return "clinical_report", 0.74, reasons

    if "datenerfassung" in lower:
        reasons.append("Datenerfassung-Marker gefunden")
        return "data_capture", 0.78, reasons
    return "other", 0.5, ["kein spezifischer Marker"]


def segment_pages(pages: list[PageText]) -> list[DocumentSegment]:
    page_classes = [(page.page, *classify_page(page.text)) for page in pages]
    if not page_classes:
        return []

    segments: list[DocumentSegment] = []
    current_type = page_classes[0][1]
    start_page = page_classes[0][0]
    end_page = start_page
    confidences = [page_classes[0][2]]
    reasons = list(page_classes[0][3])

    for page_no, segment_type, confidence, page_reasons in page_classes[1:]:
        if segment_type == current_type and page_no == end_page + 1:
            end_page = page_no
            confidences.append(confidence)
            reasons.extend(page_reasons)
            continue

        segments.append(_make_segment(len(segments) + 1, current_type, start_page, end_page, confidences, reasons))
        current_type = segment_type
        start_page = page_no
        end_page = page_no
        confidences = [confidence]
        reasons = list(page_reasons)

    segments.append(_make_segment(len(segments) + 1, current_type, start_page, end_page, confidences, reasons))
    return segments


def _make_segment(
    index: int,
    segment_type: str,
    start_page: int,
    end_page: int,
    confidences: list[float],
    reasons: list[str],
) -> DocumentSegment:
    unique_reasons = list(dict.fromkeys(reasons))
    return DocumentSegment(
        segment_id=f"seg-{index:03d}",
        segment_type=segment_type,
        title=SEGMENT_LABELS.get(segment_type, segment_type),
        start_page=start_page,
        end_page=end_page,
        relevant_for_billing=segment_type in RELEVANT_TYPES,
        confidence=round(sum(confidences) / max(len(confidences), 1), 2),
        reasons=unique_reasons[:5],
    )
