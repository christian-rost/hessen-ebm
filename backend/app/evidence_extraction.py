from __future__ import annotations

import hashlib
import re
import unicodedata

from .models import DocumentSegment, Evidence, ExcludedEvidence, PageText, ReviewCandidate


DATE_RE = re.compile(r"(\d{2}\.\d{2}\.\d{4})")
TIME_RE = re.compile(r"(\d{2}:\d{2})")
ICD_RE = re.compile(r"([A-Z]\d{2}\.\d{1,2})(?![\d.])")


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


def _service_datetime(text: str, fallback: bool = True) -> tuple[str | None, str | None]:
    compact = _compact(text)
    patterns = [
        r"durchgef.{0,40}?(\d{2}\.\d{2}\.\d{4})um(\d{2}:\d{2})",
        r"probenentnahmedat\.?(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"aufnahmezna(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"aufnahme(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"termindgf:?(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"leistungam(\d{2}\.\d{2}\.\d{4})um(\d{2}:\d{2})",
        r"befundetam(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
        r"auftragsdatum(\d{2}\.\d{2}\.\d{4})(\d{2}:\d{2})",
    ]
    for pattern in patterns:
        match = re.search(pattern, compact)
        if match:
            return _date_to_iso(match.group(1)), match.group(2)

    if not fallback:
        return None, None
    return _first_date(text), _first_time(text)


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

        if segment_type in {"case_context", "treatment_report"}:
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
            evidence.extend(_extract_radiology(page, segment_type))
            evidence.extend(_extract_labs(page, segment_type, last_lab_datetime))

        evidence.extend(_extract_internal_service_hints(page, segment_type))

        if segment_type in {"consult", "ecg", "data_capture", "laboratory_result", "radiology_report"}:
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


def _extract_specialty_ambulance(page: PageText, segment_type: str) -> list[Evidence]:
    if segment_type not in {"case_context", "treatment_report"}:
        return []

    text = page.text
    compact = _compact(text)
    service_date, service_time = _service_datetime(text)
    found: list[Evidence] = []

    if (
        "notfallambulanzaugenklinik" in compact
        or "notfallmassig" in compact
        or "notfallsymptomorientierteuntersuchung" in compact
        or "notfall-symptomorientierteuntersuchung" in compact
    ):
        found.append(
            _ev(
                "context.specialty_ambulance_emergency",
                "Fachambulanz-Notfallkontakt",
                page.page,
                "Notfallkontakt in der Augenambulanz / Fachambulanz dokumentiert",
                service_date,
                service_time,
                0.88,
                metadata=_search_terms(
                    "Notfallpauschale",
                    "Notfall",
                    "Augenaerztliche Grundpauschale",
                    "Augenheilkunde",
                    "Grundpauschale",
                ),
            )
        )

    if "ambulanzaugen-befund" in compact or "augenambulanz" in compact or "augenklinik" in compact:
        found.append(
            _ev(
                "clinical.ophthalmology_report",
                "Augenaerztlicher Ambulanzbefund",
                page.page,
                text,
                service_date,
                service_time,
                0.84,
                metadata=_search_terms(
                    "Augenaerztliche Untersuchung",
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
                    "Augenaerztliche Untersuchung",
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


def _extract_internal_service_hints(page: PageText, segment_type: str) -> list[Evidence]:
    if segment_type != "data_capture":
        return []

    text = page.text
    compact = _compact(text)
    service_date, service_time = _service_datetime(text)
    found: list[Evidence] = []

    if "all_ordnot" in compact or "ordinationsgebuhr(notfall)" in compact:
        found.append(
            _ev(
                "internal_service.emergency_ordination",
                "Interner Hinweis Ordinationsgebuehr Notfall",
                page.page,
                "Interner Leistungsbogen enthaelt ALL_ORDNOT / Ordinationsgebuehr Notfall",
                service_date,
                service_time,
                0.7,
                metadata=_search_terms("Notfallpauschale", "Notfall", "Ordinationsgebuehr", "Grundpauschale"),
            )
        )

    if "aua_buahg" in compact or "binokulareuntersuchungdesaugenhintergrundes" in compact:
        found.append(
            _ev(
                "internal_service.ophthalmology_fundus",
                "Interner Hinweis Augenhintergrund",
                page.page,
                "Interner Leistungsbogen enthaelt AUA_BUAHG / binokulare Untersuchung des Augenhintergrundes",
                service_date,
                service_time,
                0.68,
                metadata=_search_terms("Augenhintergrund", "Fundus", "Binokulare Untersuchung des Augenhintergrundes"),
            )
        )

    for code, label, terms in [
        ("aua_echo", "Interner Hinweis Echographie", ["Echographie", "Ultraschall Auge", "Augenheilkunde"]),
        ("aua_fag", "Interner Hinweis Fluoreszenzangiographie", ["Fluoreszenzangiographie", "Angiographie Auge"]),
        ("aua_peri", "Interner Hinweis Perimetrie", ["Perimetrie", "Gesichtsfeld"]),
    ]:
        if code in compact:
            found.append(
                _ev(
                    f"internal_service.{code}",
                    label,
                    page.page,
                    f"Interner Leistungsbogen enthaelt {code.upper()}",
                    service_date,
                    service_time,
                    0.62,
                    metadata=_search_terms(*terms),
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
        found.append(_ev("radiology.ct_head_native", "CT Kopf nativ", page.page, "CT Kopf nativ durchgefuehrt", service_date, service_time, 0.96))

    if ("rontgenschulter2eb" in compact or "roeschulter2eb" in compact) and "durchgefuhrt" in compact:
        found.append(_ev("radiology.xray_shoulder_2_planes", "Roentgen Schulter 2 Ebenen", page.page, "Roentgen Schulter 2 Ebenen durchgefuehrt", service_date, service_time, 0.96))

    if ("rontgenhws2ebenen" in compact or "roehws2ebenen" in compact) and "durchgefuhrt" in compact:
        found.append(_ev("radiology.xray_spine_hws_2_planes", "Roentgen HWS 2 Ebenen", page.page, "Roentgen HWS 2 Ebenen durchgefuehrt", service_date, service_time, 0.96))

    if ("rontgenlunge" in compact or "roelunge" in compact or "thorax" in compact) and ("2ebenen" in compact or "p.a." in text.lower()):
        found.append(_ev("radiology.xray_thorax_2_planes", "Roentgen Thorax/Lunge 2 Ebenen", page.page, "Roentgen Thorax/Lunge 2 Ebenen", service_date, service_time, 0.86))

    if ("ctlws" in compact or "ct-lws" in compact or "ctcthws" in compact or "cthws" in compact) and "durchgefuhrt" in compact and "storniert" not in compact:
        found.append(_ev("radiology.ct_spine_section", "CT Wirbelsaeulenabschnitt", page.page, "CT Wirbelsaeulenabschnitt durchgefuehrt", service_date, service_time, 0.84))

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
        ("lab.hemoglobin", "Haemoglobin", "hamoglobin"),
        ("lab.hematocrit", "Haematokrit", "hamatokrit"),
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

    if segment_type == "ecg":
        candidates.append(ReviewCandidate(evidence="12-Kanal-EKG", evidence_pages=[page.page], reason="Kein freigegebenes Mapping im aktuellen Regelset."))
    if segment_type == "consult" and "neurologie" in text.lower():
        candidates.append(ReviewCandidate(evidence="Neurologisches Konsil", evidence_pages=[page.page], reason="Interne Konsiltypen sind nicht automatisch EBM-GOPs."))
    if segment_type == "consult" and ("psych" in text.lower() or "psychische" in text.lower()):
        candidates.append(ReviewCandidate(evidence="Psychiatrisches Konsil", evidence_pages=[page.page], reason="Interne Konsiltypen sind nicht automatisch EBM-GOPs."))
    if "schwangerschaftstest" in compact or "schwangerschaftsnachweis" in compact:
        candidates.append(ReviewCandidate(evidence="Schwangerschaftstest Urin", evidence_pages=[page.page], possible_gops=["32132"], reason="Katalogtreffer moeglich, aber noch keine validierte Positivregel."))
    if "drogen" in compact and "urin" in compact:
        candidates.append(ReviewCandidate(evidence="Drogen-Screening Urin", evidence_pages=[page.page], possible_gops=["32292", "32307"], reason="Panel-/Einzeltestlogik und Abrechnungsfaehigkeit nicht validiert."))
    if "urinstatus" in compact:
        candidates.append(ReviewCandidate(evidence="Urinstatus", evidence_pages=[page.page], possible_gops=["32720"], reason="Im Goldstandard noch keine Positivregel."))
    if any(token in compact for token in ["crp", "ck-mb", "myoglobin", "harnstoff", "gamma-gt", "ast"]):
        candidates.append(ReviewCandidate(evidence="Erweiterte Laborwerte", evidence_pages=[page.page], possible_gops=["32065", "32069", "32071", "32074", "32092", "32128", "32450"], reason="Nicht jeder dokumentierte Laborwert wird automatisch abgerechnet."))

    return candidates


def _extract_exclusions(page: PageText, segment_type: str) -> list[ExcludedEvidence]:
    text = page.text
    compact = _compact(text)
    excluded: list[ExcludedEvidence] = []

    if "ctcthws" in compact and "storniert" in compact:
        excluded.append(ExcludedEvidence(evidence="CT HWS nativ", evidence_pages=[page.page], not_billed_gop="34311", reason="Nur storniert dokumentiert; kein durchgefuehrter Befund."))
    if "ctkopfnativ" in compact and "nativ" in compact:
        excluded.append(ExcludedEvidence(evidence="CT-Kontrastmittelzuschlag", evidence_pages=[page.page], not_billed_gop="34345", reason="CT als nativ dokumentiert; keine Kontrastmittelgabe."))
    if "probeunterfullt" in compact:
        excluded.append(ExcludedEvidence(evidence="Gerinnungsprobe", evidence_pages=[page.page], reason="Probe unterfuellt/falsches Mischungsverhaeltnis."))
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
