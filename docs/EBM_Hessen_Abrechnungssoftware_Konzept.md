# Konzept: EBM-/Hessen-GOP-Abrechnungssoftware

Dieses Konzept beschreibt eine Abrechnungssoftware, die aus klinischer Evidenz, z. B. `25130195.pdf`, abrechenbare GOP-Kandidaten ableitet, diese gegen den passenden quartalsversionierten EBM- und ggf. regionalen Hessen-GOP-Katalog prüft und daraus eine nachvollziehbare Rechnung erzeugt.

Als technische Referenz wurde das Repository `christian-rost/abrechnungssoftware` auf Branch `codex/goae-mvp` verwendet. Das Referenzprojekt ist ein GOÄ-MVP mit React/Vite-Frontend, FastAPI-Backend, Supabase/Postgres, Redis/Worker-Struktur, Mistral OCR/LLM, Kandidatenworkflow, Regelprüfung, User-Management und Coolify-Docker-Compose-Deployment. Das hier beschriebene System überträgt dieses Muster auf EBM und regionale GOP-Kataloge.

## Zielbild

Die Software soll nicht einfach Text aus einer PDF in eine Rechnung kopieren. Sie muss eine fachliche Kette abbilden:

```text
Klinische Evidenz
  -> PDF-Upload
  -> OCR mit Mistral OCR
  -> Dokumentsegmentierung
  -> Relevanzfilter
  -> strukturierte Evidenz
  -> Abrechnungskandidaten
  -> Katalogvalidierung nach Leistungsquartal
  -> Regelprüfung
  -> Sachbearbeiterentscheidung
  -> finale Rechnung
  -> standardisiertes Ausgabeformat
```

Leitprinzipien:

- Das Behandlungsdatum bestimmt den Katalogstand, nicht das Rechnungsdatum.
- Bundesweiter EBM und regionale Kataloge bleiben getrennte Referenzschichten.
- Jede vorgeschlagene GOP braucht Evidenz, Regelbezug und Katalogtreffer.
- LLM/OCR darf Vorschläge unterstützen, aber nicht ohne Regel- und Sachbearbeiterprüfung final abrechnen.
- Die Rechnung muss später reproduzierbar sein, auch wenn neue Quartalskataloge importiert wurden.

## Fachlicher Scope

### Eingaben

| Eingabe | Zweck |
| --- | --- |
| Klinische PDF | Primärer Eingangskanal; kann mehrere Dokumente enthalten, z. B. Arztbrief, Behandlungsbericht, Radiologie, Labor, Aufträge, Befunde |
| Technische Rechnungs-/KVDT-/ADT-Datei | Vergleich, Import bestehender Abrechnungsdaten, Goldstandard |
| EBM-KBV-Katalog | Bundesweite quartalsversionierte GOP-Referenz |
| Hessen-GOP-Katalog | Regionale quartalsversionierte Zusatzreferenz |
| Patienten-/Fallstammdaten | Patient, Kostenträger, VKNR, Behandlungsfall, Scheinart, Praxis/KV |

### Ausgaben

| Ausgabe | Zweck |
| --- | --- |
| GOP-Kandidaten | Prüfvorschläge für Sachbearbeitung |
| Regelprüfung | Fehler, Warnungen, fehlende Evidenz, Ausschlüsse |
| Rechnungsentwurf | angenommene GOPs, Beträge, Begründungen |
| Standardisierte Abrechnungsdatei | Maschinenlesbarer Export für das Zielsystem, z. B. KVDT-/ADT-artig oder ein verbindlich festgelegtes Zielprofil |
| Audit-Trail | Nachvollziehbarkeit jeder Entscheidung |

## Referenzarchitektur

Das Zielsystem orientiert sich an der GOÄ-Referenz:

```text
React/Vite Frontend
        |
        v
FastAPI Backend
        |
        +--> Supabase/Postgres
        |      - User / Rollen
        |      - Patienten / Fälle
        |      - Dokumente / OCR-Seiten
        |      - Evidenz
        |      - Kataloge
        |      - Regeln
        |      - Kandidaten
        |      - Rechnungen
        |
        +--> Redis Queue
        +--> Worker
        |      - OCR
        |      - Extraktion
        |      - Kandidatengenerierung
        |      - Reanalyse
        |
        +--> Mistral OCR
        +--> optional Mistral LLM
```

Der bestehende lokale SQLite-Katalog `ebm_kbv.sqlite` ist für Analyse und Prototyping geeignet. Für die eigentliche Anwendung sollte der Katalog in Supabase/Postgres migriert oder regelmäßig aus SQLite nach Postgres synchronisiert werden.

## Laufzeit und Infrastruktur

Die Zielruntime sollte wie im Referenzprojekt über Coolify und Docker Compose laufen.

Services:

| Service | Aufgabe |
| --- | --- |
| `frontend` | React/Vite-Oberfläche für Sachbearbeitung, Admin, Rechnungsentwurf |
| `api` | FastAPI: Auth, Dokumentimport, Katalogsuche, Kandidaten, Regelprüfung, Rechnung |
| `worker` | Hintergrundjobs für OCR, Extraktion, Reanalyse, Katalogimport |
| `redis` | Queue und Jobstatus |
| `supabase` | Externe Postgres-Datenbank, Storage, optional Auth/Realtime |
| `ocr/llm provider` | Mistral OCR und optional Mistral Chat API |

Wichtige Environment-Variablen:

```env
ENVIRONMENT=production
API_PORT=8000
JWT_SECRET=<long-random-secret>
ADMIN_USERNAME=admin
ADMIN_PASSWORD=<secure-password>
ALLOW_SELF_REGISTRATION=false
CORS_ORIGINS=https://ebm.example.de

SUPABASE_URL=https://supabase.example.de
SUPABASE_KEY=<server-side-key>
SUPABASE_SCHEMA=public
DOCUMENT_STORAGE_PROVIDER=supabase

VITE_API_BASE=https://ebm-api.example.de

OCR_PROVIDER=mistral
MISTRAL_API_KEY=<mistral-api-key>
MISTRAL_OCR_MODEL=mistral-ocr-latest
MISTRAL_OCR_ENDPOINT=https://api.mistral.ai/v1/ocr

LLM_PROVIDER=mistral
LLM_MODEL=mistral-large-latest
LLM_API_KEY=

REDIS_URL=redis://redis:6379/0
```

Datenschutz:

- Server-side API-Keys dürfen nie ins Frontend.
- Originaldokumente liegen in Supabase Storage oder S3-kompatiblem Storage.
- OCR/LLM-Verarbeitung braucht DPA/TOMs, Löschkonzept und optional Pseudonymisierung.
- Jede Sachbearbeiterentscheidung wird revisionssicher protokolliert.

## PDF-Eingangskanal und Dokumentsegmentierung

Der primäre Eingangskanal ist der Upload genau einer PDF-Datei pro Fall oder Vorgang. Diese PDF kann mehrere fachlich unterschiedliche Dokumente enthalten. Im Beispiel `25130195.pdf` sind u. a. enthalten:

- Indikationsprüfungen
- Radiologieanforderungen
- ZNA-Behandlungsbericht
- Radiologiebefunde
- Radiologie-Datenerfassungsseiten
- Laborbefunde
- ToDo-/Pflege-/Auftragsdokumente
- leere oder bildhafte Seiten

Die Anwendung darf deshalb nicht das komplette PDF gleichberechtigt für die Abrechnung auswerten. Stattdessen wird aus dem PDF zuerst eine Dokumentstruktur erzeugt.

### Verarbeitungsschritte

```text
PDF hochladen
  -> Originaldatei speichern
  -> Seiten extrahieren
  -> OCR je Seite mit Mistral OCR
  -> Seitentexte speichern
  -> Seiten zu Dokumentsegmenten gruppieren
  -> Dokumentsegmente klassifizieren
  -> Relevanz je Dokumentsegment bestimmen
  -> nur relevante Segmente an die Evidenzextraktion geben
```

### Dokumentsegment

Ein Dokumentsegment ist eine zusammenhängende Menge von PDF-Seiten, die fachlich ein Dokument bilden.

Beispiele:

| Segmenttyp | Beispiel | Relevanz für Evidenz |
| --- | --- | --- |
| `treatment_report` | ZNA-Behandlungsbericht | hoch |
| `radiology_report` | Radiologie-Befund | hoch |
| `lab_report` | Laborbefund | hoch |
| `radiology_order` | Anforderung Radiologie | mittel, Kontext/Indikation |
| `radiology_acquisition` | Datenerfassung Radiologie | hoch für technische Durchführung, KM, Ebenen |
| `medication_material` | Kontrastmittelblatt | hoch für Material-/KM-Evidenz |
| `administrative` | Deckblatt, Empfänger, Stammdaten | niedrig |
| `task_note` | ToDo/Pflegeauftrag | niedrig bis mittel |
| `empty_or_image_only` | leere Seite/Scan ohne Text | keine |

### Relevanzfilter

Nur freigegebene Segmenttypen werden für die GOP-Evidenzsuche genutzt.

```text
Evidenzrelevant:
  treatment_report
  radiology_report
  lab_report
  radiology_acquisition
  medication_material

Kontextrelevant:
  radiology_order
  administrative mit Fall-/Kostenträgerdaten

Nicht direkt abrechnungsrelevant:
  task_note ohne Leistungsnachweis
  leere Seiten
  reine Verwaltungsseiten
  interne technische Zuschlags-/Workflowseiten ohne GOP-Mapping
```

Der Relevanzfilter ist bewusst konservativ. Ein Dokumentsegment kann Informationen für den Fallkontext liefern, ohne selbst GOP-Kandidaten erzeugen zu dürfen.

### Dokumentklassifikation

Die Klassifikation erfolgt zweistufig:

1. Regelbasiert über stabile Marker im OCR-Text.
2. Optional LLM-gestützt, wenn Marker nicht eindeutig sind.

Beispiele für Marker:

| Marker | Segmenttyp |
| --- | --- |
| `Behandlungsbericht ZNA` | `treatment_report` |
| `Radiologie - Befund` | `radiology_report` |
| `Laborbefund` | `lab_report` |
| `Datenerfassung` plus `Radiologie` | `radiology_acquisition` |
| `Kontrastmittel` | `medication_material` |
| `Indikationsprüfung` | `radiology_order` |
| `ToDo erledigt` | `task_note` |

Die Klassifikation wird gespeichert und im Frontend sichtbar gemacht. Sachbearbeiter können Segmenttyp und Relevanz korrigieren. Diese Korrekturen werden auditierbar protokolliert und bei Reanalysen berücksichtigt.

## Datenmodell

### Kernobjekte

| Tabelle | Bedeutung |
| --- | --- |
| `patients` | Patientendaten, Versicherten-ID, Geburtsdatum, Geschlecht |
| `encounters` | Behandlungsfall mit Behandlungsdatum, Quartal, KV-Region, Scheinart |
| `clinical_documents` | Hochgeladene PDFs/TXT-Dateien, Dokumenttyp, Status |
| `document_pages` | OCR-/Extraktionstext je Seite |
| `document_segments` | Fachlich zusammenhängende Dokumente innerhalb einer PDF |
| `clinical_evidence` | Strukturierte Evidenzatome aus Dokumenten |
| `diagnoses` | Diagnosen mit ICD-10-GM, Sicherheit, Quelle |
| `procedures` | Prozeduren/OPS/lokale Leistungskennungen |
| `billing_candidates` | Vorschläge vor finaler Annahme |
| `billing_items` | final akzeptierte Abrechnungspositionen |
| `invoices` | Rechnungsentwurf/finale Rechnung |
| `invoice_exports` | Standardisierte Ausgabe, Exportstatus, Prüfsummen, Dateipfade |
| `audit_events` | Änderungen und Entscheidungen |

### Katalogmodell

EBM und Hessen-GOP sollten in einem gemeinsamen Referenzmodell abgebildet werden, aber mit klarer Quellenkennung.

| Tabelle | Bedeutung |
| --- | --- |
| `catalog_snapshots` | Ein Katalogstand pro Quelle, Quartal und Region |
| `catalog_entries` | GOPs aus EBM oder regionalem Katalog |
| `catalog_entry_sections` | Abschnitte/Kapitel |
| `catalog_rules` | Katalogregeln, Ausschlüsse, Häufigkeiten, Voraussetzungen |
| `catalog_edges` | Graphartige Beziehungen zwischen GOPs |
| `catalog_payers` | Kassen-/VKNR- und Vertragsbeschränkungen |

Wichtige Felder in `catalog_entries`:

```text
catalog_id
source_system        EBM_KBV | KV_HESSEN_GOP
quarter              2025/Q4
region               NULL | Hessen
gop_original
gop_code
gop_base
gop_suffix
markers
title
description
points
euro
raw_text
```

Regel:

```text
EBM_KBV        -> bundesweit, region = NULL
KV_HESSEN_GOP  -> regional, region = Hessen
```

## Evidenzmodell

Die Software sollte keine GOP direkt aus unstrukturiertem Text erzeugen. Dazwischen liegt eine Evidenzschicht.

Beispiele:

| evidence_type | Inhalt |
| --- | --- |
| `case_context` | ZNA, KV-Notfall, ambulant, stationär, Fachrichtung |
| `lab_result` | Laborparameter, Wert, Einheit, Referenzbereich, Probenzeit |
| `imaging_study` | Modalität, Region, Ebenen, Kontrastmittel, Durchführungszeit |
| `procedure` | OPS/lokaler Leistungscode, Prozedurbeschreibung |
| `diagnosis` | ICD oder Freitextdiagnose |
| `medication_material` | Kontrastmittel, Sachmittel, Verbrauchsmaterial |
| `negative_evidence` | dokumentiert, aber nicht abrechnungsrelevant |

Jedes Evidenzatom speichert:

```text
encounter_id
document_id
segment_id
page
evidence_type
normalized_name
value_json
source_text
confidence
extractor_version
```

`clinical_evidence.segment_id` ist wichtig: Evidenz darf nur aus Segmenten entstehen, die als relevant oder kontextrelevant markiert sind. So wird verhindert, dass administrative Seiten, Aufgabenlisten oder interne technische Angaben versehentlich GOPs erzeugen.

## Regel- und Kandidatenlogik

### Pipeline

```text
1. PDF hochladen
2. OCR je Seite mit Mistral OCR ausführen
3. Seiten zu Dokumentsegmenten gruppieren
4. Dokumentsegmente klassifizieren
5. Relevante und kontextrelevante Segmente bestimmen
6. Evidenzatome nur aus freigegebenen Segmenten extrahieren
7. Evidenz normalisieren
8. Katalogkandidaten über EBM-/Hessen-GOP-Retrieval und freigegebene Fallback-Regeln erzeugen
9. Semantische LLM-Herleitung aus Evidenz und Katalogkandidaten ausführen
10. LLM-Vorschläge serverseitig gegen Kandidatenpool und Quartalskatalog validieren
11. Sachbearbeiter entscheidet
12. Rechnung erzeugen
13. Rechnung im standardisierten Ausgabeformat exportieren
```

### Semantische LLM-Herleitung

Die GOP wird nicht mehr direkt über ein starres Evidence-zu-GOP-Mapping erzeugt. Der Standardpfad ist:

1. Evidenzatome enthalten Typ, Label, Seite, Leistungsdatum/-zeit und Textauszug.
2. Der Server erzeugt einen begrenzten GOP-Kandidatenpool aus dem aktiven Quartalskatalog.
3. Das LLM erhält nur Evidenz und diesen Kandidatenpool.
4. Das LLM liefert ein strukturiertes JSON mit:
   - `items`: vorgeschlagene Rechnungspositionen
   - `review_candidates`: unsichere oder fachlich zu prüfende Kandidaten
   - `excluded_evidence`: bewusst nicht übernommene Evidenz
5. Der Server übernimmt nur GOPs, die im Kandidatenpool enthalten sind und im aktiven Katalog validiert werden.

Die deterministischen Regeln bleiben als Fallback erhalten, wenn kein LLM-Key konfiguriert ist oder die LLM-Antwort nicht verwertbar ist. Jede akzeptierte Position muss eine Evidenzreferenz, Katalogquelle und Herleitungsbegründung tragen.

### Regeldefinition

Regeln sollten versioniert in der Datenbank liegen, nicht nur im Code. In der LLM-basierten Pipeline dienen sie vor allem als Guardrail und als Quelle fuer bereits validierte Katalogkandidaten.

Beispielstruktur:

```json
{
  "rule_id": "lab.quick.to.32113.v1",
  "source_system": "EBM_KBV",
  "quarter_scope": "*",
  "evidence_type": "lab_result",
  "match": {
    "normalized_name": ["quick", "quick-wert"]
  },
  "candidate": {
    "gop_code": "32113",
    "reason": "Quick-Wert im Laborbefund dokumentiert"
  },
  "required_context": {
    "care_setting": ["ambulant", "notfall"]
  },
  "negative_context": []
}
```

Jeder Treffer schreibt einen Datensatz in `billing_rule_matches`, damit später sichtbar bleibt, warum ein Kandidat existiert.

### Kandidatenstatus

Wie im GOÄ-Referenzprojekt:

| Status | Bedeutung |
| --- | --- |
| `suggested` | automatisch vorgeschlagen |
| `accepted` | von Sachbearbeitung übernommen |
| `rejected` | abgelehnt |
| `needs_review` | unklar, Rückfrage nötig |
| `blocked` | Regelproblem verhindert Abrechnung |

`billing_candidates` enthält:

```text
encounter_id
catalog_id
source_system
region
quarter
gop_original
gop_code
gop_base
title
points
euro
quantity
status
confidence
evidence_ids[]
rule_match_ids[]
candidate_json
```

## Beispielregeln aus Fall 25130195

Aus `25130195.pdf` und `Rechnung 25130195.txt` ergeben sich direkt nutzbare Regeln:

| Evidenz | Kandidat | Begründung |
| --- | --- | --- |
| KV-Notfall/ZNA | `01210` | Notfallpauschale |
| Röntgen Lunge/Thorax 2 Ebenen | `34241` | Übersichtsaufnahme Brustorgane, zwei Ebenen |
| CT CTLWS/LWS/Teil-Wirbelsäule | `34311` | CT von Teilen der Wirbelsäule |
| CT plus i.v. Kontrastmittel | `34345` | Zuschlag Kontrastmitteluntersuchung |
| Quick | `32113` | Quick-Wert, Plasma |
| ALT/GPT | `32070` | GPT |
| Kreatinin | `32066` | Kreatinin |
| Natrium | `32083` | Natrium |
| Kalium | `32081` | Kalium |
| Glucose | `32025` | Glucose |
| Erythrozyten | `32035` mit Originalsuffix `A` |
| Leukozyten | `32036` mit Originalsuffix `A` |
| Thrombozyten | `32037` mit Originalsuffix `A` |
| Hämoglobin | `32038` mit Originalsuffix `A` |
| Hämatokrit | `32039` mit Originalsuffix `A` |

Gleichzeitig zeigen die Dokumente wichtige Negativregeln:

| Evidenz in PDF | Ergebnis |
| --- | --- |
| CRP | nicht automatisch abrechnen |
| INR/APTT | nicht automatisch abrechnen |
| Urinstatus | nicht automatisch abrechnen |
| interne Radiologiecodes wie Dringlichkeit, Befunddemonstration, Venenverweilkanüle | nicht automatisch als EBM-GOP übernehmen |

Merksatz:

```text
Dokumentierte Evidenz erzeugt nur dann eine GOP,
wenn ein freigegebenes Mapping plus Kontextregel plus Katalogtreffer existiert.
```

## Katalogauswahl

Katalogauswahl pro Kandidat:

```text
service_date -> quarter
encounter.region -> region

1. Suche in EBM_KBV mit quarter.
2. Wenn kein Treffer oder regionale Quelle erforderlich:
   Suche in KV_HESSEN_GOP mit quarter + region.
3. Wenn mehrere Treffer:
   Quelle, Regel, Kontext und Sachbearbeiterentscheidung speichern.
```

Für Fall `25130195`:

```text
service_date = 15.10.2025
quarter      = 2025/Q4
region       = Hessen
EBM Treffer  = 15/15
Hessen GOP   = 0/15
```

## Regelprüfung

Die Regelprüfung läuft nach jeder Kandidatenänderung und vor Rechnungsfreigabe.

Prüfklassen:

| Klasse | Beispiele |
| --- | --- |
| `catalog_existence` | GOP existiert im passenden Quartal |
| `source_context` | EBM oder Hessen-GOP korrekt gewählt |
| `required_evidence` | CT-Zuschlag nur mit CT und Kontrastmittelgabe |
| `frequency_limit` | einmal je Behandlungsfall/Tag/Sitzung |
| `exclusion` | nicht neben anderer GOP |
| `payer_restriction` | Hessen-GOP nur bei bestimmter Kasse/VKNR |
| `region_restriction` | regionale GOP nur in Hessen |
| `quarter_validity` | Katalogstand passt zum Leistungsdatum |
| `suffix_handling` | `32035A` validiert gegen Basis-GOP `32035` |
| `missing_required_context` | z. B. Notfallpauschale ohne Notfallkontext |

Issue-Beispiel:

```json
{
  "type": "required_evidence",
  "severity": "error",
  "codes": ["34345"],
  "message": "GOP 34345 benötigt eine passende CT-Hauptleistung und dokumentierte Kontrastmittelgabe."
}
```

## Standardisiertes Ausgabeformat

Nach der Freigabe erzeugt die Anwendung nicht nur eine visuelle Rechnung, sondern eine maschinenlesbare standardisierte Ausgabedatei. Dieses Exportformat ist ein eigener fachlicher Vertrag zwischen der Abrechnungssoftware und dem nachgelagerten System.

Für den MVP sollte der Export als versioniertes Profil modelliert werden:

```text
export_profile = EBM_KVDT_ADT_LIKE_V1
```

Das Profil ist bewusst als Platzhalter benannt, bis das verbindliche Zielsystem und die exakte Spezifikation feststehen. Naheliegend ist ein KVDT-/ADT-artiges Format, weil die vorhandene Datei `Rechnung 25130195.txt` bereits eine solche feldkennungsbasierte Struktur zeigt.

### Exportstufen

```text
accepted billing_items
  -> invoice
  -> export_projection
  -> format validation
  -> export_file
  -> checksum + audit event
```

### Exportinhalt

Der Export muss mindestens enthalten:

| Bereich | Inhalt |
| --- | --- |
| Patient | Patientennummer, Versicherten-ID, Geburtsdatum, Geschlecht |
| Kostenträger | Kostenträgerkennung, VKNR, Kassenname, Abrechnungsbereich |
| Fall | Behandlungsquartal, Behandlungstag, Scheinart, Fachrichtung, Region |
| Leistungen | GOP original, GOP base, Suffix, Leistungstag, Uhrzeit, Menge, Begründungstext |
| Diagnosen | ICD-10-GM, Diagnosesicherheit, Diagnosekontext |
| Katalogbezug | Katalogquelle, Quartal, Region, Snapshot-ID |
| Nachvollziehbarkeit | Evidenzreferenzen, Regelmatches, Sachbearbeiterfreigabe |

### Exportvalidierung

Vor dem Schreiben der Datei wird geprüft:

- alle akzeptierten GOPs existieren im verwendeten Katalogsnapshot
- jedes Leistungsdatum passt zum Abrechnungsquartal
- regionale GOPs enthalten Region und ggf. VKNR-/Kassenkontext
- GOP-Suffixe sind getrennt als `gop_original`, `gop_base`, `gop_suffix` gespeichert
- Pflichtfelder des Zielprofils sind vorhanden
- Summen und Positionsanzahl sind reproduzierbar
- Exportdatei erhält Prüfsumme und unveränderlichen Audit-Eintrag

### Trennung von Rechnung und Export

Die sichtbare Rechnung und die standardisierte Ausgabedatei sind unterschiedliche Artefakte:

| Artefakt | Zweck |
| --- | --- |
| Rechnungsentwurf | Sachbearbeiterprüfung, UI, Druck/PDF |
| Standardexport | Übergabe an Abrechnungssystem, Archivierung, Weiterverarbeitung |

Beide Artefakte verweisen auf dieselben `billing_items`, dürfen aber unterschiedliche Darstellungen haben.

## Sachbearbeiter-Oberfläche

Die Oberfläche sollte die Logik des Referenzprojekts übernehmen:

1. PDF hochladen
2. OCR-Status und Seiten anzeigen
3. Erkannte Dokumentsegmente prüfen
4. Segmenttyp und Evidenzrelevanz bei Bedarf korrigieren
5. Extrahierte Evidenz aus relevanten Segmenten anzeigen
6. GOP-Kandidaten mit Begründung anzeigen
7. Kandidat annehmen, ablehnen, bearbeiten
8. Manuelle Katalogsuche
9. Regelprüfung anzeigen
10. Rechnung erzeugen
11. Standardisierten Export erzeugen und herunterladen

Kandidatenkarte:

```text
GOP 34345  Zuschlag Kontrastmitteluntersuchung
Quelle: EBM_KBV 2025/Q4
Evidenz:
  - CT CTLWS + KM, Seite 24
  - KM-Gabe intravenös, Seite 27
  - Imeron 400 60 ml i.v., Seite 28
Status: suggested
Aktionen: Annehmen | Ablehnen | Code ändern
```

## API-Konzept

Angelehnt an das Referenzprojekt:

| Endpoint | Zweck |
| --- | --- |
| `POST /api/auth/login` | Login |
| `GET /api/auth/me` | aktueller Benutzer |
| `POST /api/cases` | Fall anlegen |
| `GET /api/cases` | Fallliste |
| `POST /api/cases/{case_id}/documents/upload` | PDF hochladen und Analysejob starten |
| `GET /api/documents/{document_id}/pages` | OCR-/Textseiten |
| `GET /api/documents/{document_id}/segments` | erkannte Dokumentsegmente |
| `PATCH /api/document-segments/{segment_id}` | Segmenttyp/Relevanz korrigieren |
| `GET /api/cases/{case_id}/evidence` | strukturierte Evidenz |
| `POST /api/cases/{case_id}/reanalyze` | Reanalyse starten |
| `GET /api/cases/{case_id}/candidates` | GOP-Kandidaten |
| `PATCH /api/candidates/{candidate_id}` | Kandidat annehmen/ablehnen/bearbeiten |
| `GET /api/cases/{case_id}/billing-check` | Regelprüfung |
| `GET /api/catalog/search` | EBM/Hessen-GOP-Suche |
| `POST /api/invoices` | Rechnungsentwurf erzeugen |
| `GET /api/invoices/{invoice_id}` | Rechnung anzeigen |
| `POST /api/invoices/{invoice_id}/exports` | standardisierten Export erzeugen |
| `GET /api/invoice-exports/{export_id}/download` | Exportdatei herunterladen |
| `POST /api/admin/catalog/import/ebm` | EBM-Quartal importieren |
| `POST /api/admin/catalog/import/hessen-gop` | Hessen-GOP-PDF importieren |

## Module im Backend

Analog zum GOÄ-MVP:

```text
backend/app/
  main.py
  auth.py
  document_service.py
  ocr/mistral_ocr.py
  document_segmentation.py
  document_classification.py
  case_service.py
  evidence_extraction.py
  lab_extraction.py
  imaging_extraction.py
  diagnosis_extraction.py
  catalog_import_ebm.py
  catalog_import_hessen_gop.py
  catalog_repository.py
  catalog_retrieval.py
  billing_rules.py
  billing_candidates.py
  billing_check.py
  invoice_service.py
  export_service.py
  export_profiles/
    ebm_kvdt_adt_like_v1.py
  audit.py
  worker.py
```

## Migrationen

Vorgeschlagene Supabase-Migrationen:

```text
001_users_and_audit.sql
002_cases_patients_encounters.sql
003_documents_and_pages.sql
004_document_segments.sql
005_catalog_snapshots.sql
006_catalog_entries.sql
007_catalog_rules_edges_payers.sql
008_clinical_evidence.sql
009_diagnoses_procedures.sql
010_billing_candidates.sql
011_billing_checks.sql
012_invoices.sql
013_invoice_exports.sql
014_rule_definitions_and_matches.sql
```

Bestehende lokale Datenquellen:

| Datei | Ziel |
| --- | --- |
| `ebm_kbv.sqlite` | Quelle für `EBM_KBV`-Katalogsnapshots |
| `scripts/scrape_ebm_kbv.py` | Import/Refresh bundesweiter EBM-Quartale |
| `scripts/import_hessen_gop_pdf.py` | Import regionaler Hessen-GOP-Kataloge |
| `Rechnung_25130195_Regelableitung.md` | erster Goldstandard/Regelkandidat |

## Validierung

Wie im GOÄ-Referenzprojekt sollte es Goldstandardfälle geben.

Für jeden Validierungsfall:

```text
input/
  evidence.pdf
  optional_invoice.txt
expected/
  expected_candidates.json
  expected_billing_items.json
  expected_issues.json
```

Metriken:

| Metrik | Bedeutung |
| --- | --- |
| Candidate Recall | wurden alle erwarteten GOPs vorgeschlagen? |
| Candidate Precision | wie viele falsche Kandidaten wurden vorgeschlagen? |
| Billing Accuracy | stimmen akzeptierte Positionen und Summen? |
| Rule Accuracy | wurden Fehler/Warnungen korrekt erkannt? |
| Explainability | hat jeder Kandidat Evidenz und Regelbezug? |

Fall `25130195` wird erster Goldstandard:

```text
expected GOPs:
01210, 32113, 32070, 32083, 32025,
32035A, 32036A, 32037A, 32038A, 32039A,
32066, 32081, 34241, 34311, 34345
```

## MVP-Umsetzungsplan

### Phase 1: Katalog- und Fallbasis

- Supabase-Schema für EBM/Hessen-GOP anlegen
- vorhandene SQLite-Daten in Postgres importieren
- Fall-/Dokument-/Patientenmodell anlegen
- Katalogsuche über EBM und Hessen-GOP bereitstellen

### Phase 2: Dokumentimport und Evidenz

- PDF/TXT-Upload
- PDF als Container mehrerer Dokumentsegmente behandeln
- OCR mit Mistral OCR je Seite
- OCR/Textseiten speichern
- Dokumentsegmente erkennen, klassifizieren und gruppieren
- Relevanzfilter für Evidenzsuche anwenden
- Evidenzextraktion nur aus relevanten Segmenten für Labor, Radiologie, Fallkontext
- Seiten- und Quelltextbezug in UI anzeigen

### Phase 3: Regelbasierte Kandidaten

- Regeldefinitionen für Fall `25130195`
- Kandidatengenerierung
- Katalogvalidierung nach Quartal/Region
- Kandidatenworkflow: annehmen, ablehnen, bearbeiten

### Phase 4: Regelprüfung und Rechnung

- Regelprüfung für Existenz, Kontext, Zuschläge, Suffixe, Region, Quartal
- Rechnungsentwurf mit Summen
- standardisiertes Ausgabeprofil definieren und implementieren
- Exportvalidierung mit Pflichtfeldern, Katalogbezug und Prüfsumme
- Audit-Trail für Entscheidungen
- Goldstandardvergleich

### Phase 5: Produktivhärtung

- Worker asynchron aktivieren
- vollständiger Katalogimport je Quartal
- Datenschutz-/Löschkonzept
- Rollen/Rechte finalisieren
- finales Standardformat mit Zielsystem abstimmen
- Monitoring und Healthchecks erweitern

## Wesentliche Architekturentscheidung

Die Software sollte eine deterministische, erklärbare Abrechnungsmaschine sein, die KI nur als Hilfsmittel nutzt.

Gute Aufgaben für KI:

- OCR
- Dokumenttyp-Erkennung
- Extraktion unstrukturierter Evidenz
- Synonyme erkennen, z. B. `ALT` als `GPT`
- Vorschlagsranking

Nicht alleinige Aufgabe für KI:

- finale GOP-Auswahl
- Ausschlussprüfung
- Quartalsversionierung
- Kassen-/Regionenlogik
- Rechnungsfreigabe

Die finale Abrechnung entsteht aus:

```text
Evidenz + Regeln + Katalogstand + Sachbearbeiterentscheidung
```

Das ist der zentrale Unterschied zwischen einer Texterkennung und einer echten Abrechnungssoftware.
