# hessen-ebm

MVP für eine EBM-/Hessen-GOP-Abrechnungssoftware.

Die Anwendung nimmt ein klinisches PDF entgegen, extrahiert Text/OCR, trennt Dokumentsegmente, nutzt nur abrechnungsrelevante Evidenz, leitet GOP-Kandidaten ab, validiert sie gegen den quartalsversionierten EBM und optional gegen Hessen-GOP und erzeugt einen maschinenlesbaren Rechnungsentwurf.

## Was aktuell umgesetzt ist

- PDF-Upload im Frontend
- Backend-Analyse mit FastAPI
- OCR/Text-Provider:
  - Mistral OCR vorbereitet und per Environment aktivierbar
  - Fallback auf eingebetteten PDF-Text via `pdfplumber`
- Dokumentsegmentierung:
  - ZNA-/Fallkontext
  - Behandlungsbericht
  - Radiologiebefund
  - Laborbefund
  - Konsil
  - EKG
  - Datenerfassung
  - sonstige Seiten
- Evidenzextraktion aus relevanten Segmenten
- semantische LLM-Herleitung von GOPs aus Evidenz und Katalogkandidaten
- datengetriebene, versionierte Regel-Engine als deterministischer Fallback und fachliche Prüfschicht
- generischer Compiler für sämtliche KBV-Detailtexte, Präambeln, Kapitelregeln, GOP-Bereiche und regionale Regeln
- versionierte Fachregeln und einzelne Regelklauseln in Supabase
- Katalogvalidierung gegen SQLite-EBM/Hessen-GOP
- JSON-Exportprofil `EBM_KVDT_ADT_LIKE_V1_DRAFT`
- persistierte Rechnungsentwürfe mit Positionsliste in Supabase/Postgres, mit lokalem JSON-Rückfall für Entwicklung
- Admin-Bereich zum Validieren und Einspielen neuer Katalogdatenbanken
- Docker-Compose für Coolify

## Wichtige Architekturentscheidung

Die EBM-/Hessen-GOP-Katalogdatenbank wird nicht ins Git-Repo gelegt. Die aktuell erzeugte Datei `ebm_kbv.sqlite` ist ca. 225 MB groß und damit für ein normales GitHub-Repo ungeeignet.

Stattdessen erwartet die Anwendung den aktiven Katalog unter:

```text
CATALOG_DB_PATH=/app/catalog/ebm_kbv.sqlite
```

In Coolify sollte dafür ein Volume nach `/app/catalog` gemountet werden. Der Admin-Bereich kann eine vorbereitete `ebm_kbv.sqlite` hochladen, validieren und an genau diesen Pfad einspielen. Lokal kann `CATALOG_DB_PATH` auch direkt auf eine vorhandene SQLite-Datei zeigen.

Beim Einspielen wird:

1. die hochgeladene Datei als SQLite-Datenbank geöffnet
2. `pragma integrity_check` ausgeführt
3. das Vorhandensein der Tabellen `snapshots`, `nodes` und `details` geprüft
4. geprüft, ob mindestens ein Snapshot und Details vorhanden sind
5. die bisherige aktive Datenbank in `STORAGE_DIR/catalog-backups` gesichert
6. die neue Datenbank atomar nach `CATALOG_DB_PATH` ersetzt

Für produktive Deployments sollte `ADMIN_TOKEN` gesetzt werden. Die Admin-Endpunkte erwarten dann den Header `X-Admin-Token`.

## Persistierte Rechnungsentwürfe

Rechnungsentwürfe werden weiterhin als JSON unter `STORAGE_DIR/analyses` abgelegt. Wenn `SUPABASE_URL` und `SUPABASE_KEY` bzw. `SUPABASE_SERVICE_ROLE_KEY` gesetzt sind, speichert das Backend den Entwurf zusätzlich in Supabase:

- `hessen_ebm_invoices`: Kopfdaten, Summen, Quartal, Diagnose und vollständiger JSON-Payload
- `hessen_ebm_invoice_items`: einzelne GOP-Positionen mit Katalogquelle, Punkten, Betrag und Herleitung

Die Migrationen liegen unter:

```text
scripts/supabase/001_hessen_ebm_invoices.sql
scripts/supabase/002_hessen_ebm_billing_rules.sql
```

`SUPABASE_SERVICE_ROLE_KEY` ist nicht erforderlich. Das Backend verwendet `SUPABASE_KEY`; dieser Schlüssel muss für die genannten Tabellen Lese- und Schreibrechte besitzen und darf nicht an das Frontend ausgeliefert werden.

## Fachregeln in Supabase

Die große SQLite-Datei bleibt die unveränderte, quartalsversionierte Rohquelle. Der Regelcompiler liest daraus alle Detaildatensätze eines Quartals und schreibt eine für die Laufzeit optimierte Fassung nach Supabase:

- `hessen_ebm_rule_sets`: aktivierbare Regelsätze je Quartal und Region, einschließlich der versionierten klinischen Dokument- und Evidenzdefinitionen
- `hessen_ebm_rule_definitions`: GOP-Regeln, GOP-Varianten, Präambeln, Kapitelregeln und allgemeine Bestimmungen mit vollständigem Quelltext
- `hessen_ebm_rule_clauses`: einzeln nachvollziehbare Bedingungen, Ausschlüsse und Prüfklauseln
- `hessen_ebm_rule_compile_runs`: erfolgreiche und fehlgeschlagene Compilerläufe

Der Compiler übernimmt jeden KBV-Detailtext und jeden regionalen Regeltext. Eindeutig interpretierbare Klauseln werden maschinell ausgeführt. Nicht hinreichend formalisierbare Texte bleiben vollständig erhalten und werden als `partial` oder `text_only` ausgewiesen; sie gelten nicht stillschweigend als automatisch geprüft.

Auch Dokumentklassifikation, klinische Begriffe, interne Leistungscodes, Zeitrollen, Evidenz-, Review- und Ausschlussregeln sind datengetrieben. Sie liegen im Startbestand `backend/app/clinical_evidence_definitions.json` und werden beim Kompilieren im `core_payload` desselben aktiven Supabase-Regelsatzes übernommen. Die ausführenden Python-Module enthalten keine fach- oder GOP-spezifischen Kandidatenlisten.

Auswahllisten in Leistungsbögen werden zeilenbezogen verarbeitet. Das Backend verbindet den Leistungscode mit dem Zustand des zugehörigen Kästchens. Bei textbasierten PDFs werden Rechtecke und diagonale Markierungen direkt aus den PDF-Vektoren gelesen; Mistral-OCR-Markdown wird zusätzlich auf konfigurierbare Checkbox-Zeichen geprüft. Nur `checked` erzeugt Evidenz. `unchecked` wird ignoriert, `ambiguous` führt zu einem Review-Eintrag. Die Zeilen-, Marker- und Geometrieparameter stehen ebenfalls in `clinical_evidence_definitions.json`; neue Listenformate oder Codes benötigen keine Python-Verzweigung.

Weicht die klinische Definitionsversion eines aktiven Supabase-Regelsatzes von der mit dem Backend ausgelieferten Version ab, verwendet das Backend bis zur nächsten Regelmigration die aktuelle lokale Definition und weist den Fallback im Regelstatus aus. So kann ein alter Supabase-Snapshot neue Extraktionslogik nicht unbemerkt zurücksetzen.

Produktiver Ablauf:

1. `001_hessen_ebm_invoices.sql` und `002_hessen_ebm_billing_rules.sql` einmal im Supabase SQL Editor ausführen.
2. `SUPABASE_URL`, `SUPABASE_KEY` und `BILLING_RULES_SOURCE=auto` in Coolify setzen.
3. Den gewünschten EBM- und Regionalkatalog im Admin-Bereich importieren.
4. Unter **Fachregeln nach Supabase** Quartal und Region auswählen und die Migration starten.
5. Nach erfolgreichem Abschluss zeigt der Katalogstatus den aktiven Regelsatz, die Definitionszahl und die maschinelle Strukturierungsquote.

Die Migration läuft als Hintergrundjob und ist deshalb nicht vom HTTP-Zeitlimit des Reverse-Proxys abhängig. Ein neuer Regelsatz wird erst nach vollständig erfolgreicher Übertragung aktiviert; der bisherige aktive Regelsatz bleibt bis dahin verfügbar.

Die Analyse, die Rechnungsübersicht und der Wiederaufruf gespeicherter Rechnungen sind über `ADMIN_TOKEN` geschützt. In der Oberfläche wird derselbe Zugriffstoken im Analyse- und Admin-Bereich verwendet.

## Lokale Entwicklung

Backend:

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export CATALOG_DB_PATH="/Users/cro/Documents/varisano - ebm Abrechnungsservice/ebm_kbv.sqlite"
export ADMIN_TOKEN="lokales-admin-passwort"
export SUPABASE_URL="https://supabase.example.de"
export SUPABASE_KEY="server-side-key"
uvicorn app.main:app --reload --port 8000
```

Frontend:

```bash
cd frontend
npm install
npm run dev
```

Docker/Coolify-nah:

```bash
cp .env.example .env
docker compose -f docker-compose.coolify.yml -f docker-compose.local.yml up --build
```

Danach ist das Frontend lokal unter `http://localhost:8080` erreichbar.

## Coolify Deployment

1. Repository `christian-rost/hessen-ebm` in Coolify verbinden.
2. Compose-Datei `docker-compose.coolify.yml` verwenden.
3. Environment setzen:
   - `CATALOG_DB_PATH=/app/catalog/ebm_kbv.sqlite`
   - `STORAGE_DIR=/app/storage`
   - `ADMIN_TOKEN=...`
   - optional `SUPABASE_URL=...`
   - `SUPABASE_KEY=...`
   - `BILLING_RULES_SOURCE=auto`
   - optional `ENABLE_MISTRAL_OCR=true`
   - `ENABLE_SEMANTIC_BILLING=true`
   - optional `MISTRAL_API_KEY=...`
   - optional `MISTRAL_LLM_MODEL=mistral-large-latest`
4. Volume für `/app/catalog` anlegen.
5. Volume für `/app/storage` anlegen.
6. Initiale oder neue `ebm_kbv.sqlite` über den Admin-Bereich hochladen.

Wichtig: Die aktuelle Katalogdatenbank ist größer als 200 MB. Das mitgelieferte Nginx-Frontend erlaubt deshalb Uploads bis 600 MB. Falls Coolify oder ein vorgelagerter Proxy eigene Limits setzt, müssen diese ebenfalls passend erhöht werden.

Das Coolify-Compose bindet keinen festen Host-Port. Das ist Absicht: Coolify routet über die generierte Frontend-Domain zum internen Container-Port `80`. Ein fester Host-Port wie `8080` kann auf Shared-Servern mit anderen Anwendungen kollidieren.

## Versionierte Fachregeln

Der normale Ableitungspfad ist semantisch:

1. Aus dem PDF werden abrechnungsrelevante Evidenzen extrahiert.
2. Evidenzen derselben Sitzung werden zu Leistungsereignissen gebündelt. Ein Datumswechsel um Mitternacht trennt eine laufende Sitzung nicht, solange der definierte zeitliche Abstand nicht überschritten wird.
3. Zeitlich deutlich getrennte Behandlungsabschnitte werden als Episoden erkannt; nur der fachlich stärkste Abschnitt fließt in den Entwurf ein, weitere Abschnitte erscheinen im Review.
4. Der Server sucht passende EBM-/Hessen-GOP-Kandidaten im aktiven Quartalskatalog.
5. Mistral Chat erhält nur die Evidenzen des primären Abschnitts und die Kandidaten und muss ein JSON mit `items`, `review_candidates` und `excluded_evidence` liefern.
6. Der Server übernimmt nur GOPs, die im bereitgestellten Kandidatenpool enthalten sind und im aktiven Katalog validiert werden können.
7. Jede Rechnungsposition enthält `derivation_source`, `semantic_reason`, Leistungsereignis, Sitzung und zeitliche Einordnung.

Der Rechnungsentwurf enthält zusätzlich eine vollständige Ereigniszeitleiste. Administrative Aufnahme, Triage beziehungsweise Ersteinschätzung, erster persönlicher Arztkontakt, weitere persönliche Arztkontakte und abrechenbare Leistungsereignisse werden getrennt dargestellt. Eine GOP wird ausschließlich über das zugehörige Leistungsereignis verknüpft. Deshalb bleibt beispielsweise eine Aufnahme um 18:50 Uhr als „Aufnahme“ sichtbar, ohne daraus automatisch eine Rechnungsposition abzuleiten. Für zeitabhängige Notfallpauschalen ist der erkannte erste persönliche Arzt-Patienten-Kontakt maßgeblich; fehlt dieser, erscheint der Fall im Review und die Aufnahmezeit wird nicht als Ersatz verwendet.

Wenn `MISTRAL_API_KEY` fehlt oder die LLM-Antwort nicht valide ist, fällt die Analyse auf die deterministische Regel-Engine zurück und schreibt den Grund in `catalog_context.analysis_warnings`.

Das fachliche Regelwerk liegt unter `backend/app/billing_rule_definitions.json`. Es ist vom Python-Code getrennt und enthält:

- direkte Zuordnungen von Evidenzarten zu GOPs
- unverbindliche Kandidatenregeln für mehrdeutige Evidenz und interne Leistungskennungen
- zeitabhängige GOP-Gruppen mit beliebig vielen Ergebnisvarianten
- datengesteuerte Sequenzregeln für Erst- und Folgekontakte
- abgeleitete GOPs und Zuschläge mit Voraussetzungen, Kriterien und Ausschlüssen
- Gültigkeitszeiträume nach Quartal sowie regionale Gültigkeit
- Einfügebeziehungen für abgeleitete Rechnungspositionen

Der generische Evaluator unterstützt unter anderem GOP-Voraussetzungen, Evidenzarten, ICD-Präfixe, Volltextmerkmale, Alter, Datum, Uhrzeit, Wochentag, Feiertage, Region, Quartal und strukturierte Metadaten. Weitere Regeln werden als Daten ergänzt; dafür ist keine neue GOP-spezifische Python-Funktion erforderlich. Beide Abrechnungspfade verwenden dasselbe Regelwerk: die deterministische Rechnungserzeugung ebenso wie die Prüfung und Nachbearbeitung der LLM-Vorschläge.

Produktiver Python-Code enthält keine konkreten GOP-Zuordnungen. Direkte Abrechnung, Kandidatenlisten, zeitliche Varianten und Zuschläge werden ausschließlich aus dem versionierten Regelwerk beziehungsweise dem Quartalskatalog geladen. Ein Architekturtest verhindert, dass konkrete GOP-Literale erneut in `backend/app/*.py` eingeführt werden.

Die zeitliche Regelschicht dedupliziert nicht mehr pauschal fallweit nach GOP. Sie verwendet den Schlüssel aus GOP und Leistungsereignis. Dadurch kann beispielsweise `01786` an zwei verschiedenen Behandlungstagen zweimal vorkommen, während CTG-Start, CTG-Ende, Kurve und Verlaufsnotiz derselben Sitzung nur eine Position erzeugen. Für Kontaktsequenzen gilt zusätzlich: Pro Sequenzereignis entsteht höchstens eine Basispauschale. Eine laufende Notfallsitzung über Mitternacht erzeugt daher keine zweite `01212`; nur ein belegter weiterer Kontakt wird in die passende Folgekonsultations-GOP überführt. Katalogausschlüsse und Häufigkeitsgrenzen werden entsprechend ihrem Geltungsbereich pro Sitzung, Behandlungstag, Behandlungsfall oder Quartal geprüft.

Der Regelcompiler trennt Abrechnungshäufigkeit und Leistungsdauer. Formulierungen wie „einmal im Behandlungsfall“, „einmal im Krankheitsfall“, „je Sitzung“ und „einmal am Behandlungstag“ werden als Häufigkeitsgrenzen mit eigenem Bezugsraum gespeichert. Eine Zeitvorgabe entsteht ausschließlich aus einer ausdrücklichen Legendenformulierung wie „mindestens … Minuten“ oder „je vollendete … Minuten“; letztere wird als Zeitstaffel und nicht als Pauschalen-Häufigkeit modelliert.

Bei `BILLING_RULES_SOURCE=auto` bleiben die aktiven Supabase-Regeln maßgeblich. Neue lokale Kernregeln werden bis zur nächsten Admin-Kompilierung ergänzend eingeblendet, wenn ihre Regel-ID in Supabase noch fehlt. `POST /api/admin/rules/compile` schreibt anschließend das vollständige Kernregelwerk einschließlich der Kandidaten-, Ereignis- und Sequenzregeln nach Supabase.

Aktuell enthält das Regelwerk unter anderem folgende direkte Evidenzzuordnungen:

| Evidenz | GOP |
| --- | --- |
| KV-Notfall/ZNA | `01210` |
| CTG / Kardiotokografie | `01786` |
| Sonografie der mütterlichen Nieren / des Retroperitoneums | `33042` |
| Quick | `32113` |
| Kreatinin | `32066` |
| Natrium | `32083` |
| Kalium | `32081` |
| Glucose | `32025` |
| ALT/GPT | `32070` |
| Erythrozyten | `32035A` |
| Leukozyten | `32036A` |
| Thrombozyten | `32037A` |
| Hämoglobin | `32038A` |
| Hämatokrit | `32039A` |
| Röntgen Thorax/Lunge 2 Ebenen | `34241` |
| CT Wirbelsäulenabschnitt | `34311` |
| CT mit Kontrastmittel | `34345` |
| CT Kopf nativ | `34310` |
| Röntgen Schulter 2 Ebenen | `34231` |
| Röntgen HWS 2 Ebenen | `34221` |

Die Zeitvarianten der Notfall-GOPs und der Zuschlag `01226` stehen ebenfalls ausschließlich im versionierten Regelwerk. `GET /api/rules` liefert die Regelwerk-ID und -Version sowie direkte, unverbindliche, zeitabhängige und abgeleitete Regeln.

## API

| Endpoint | Zweck |
| --- | --- |
| `GET /health` | Healthcheck |
| `GET /api/catalog/status` | Katalogstatus |
| `GET /api/catalog/search?q=...&quarter=2025/Q4` | EBM-/Hessen-GOP-Suche |
| `GET /api/admin/catalog/status` | Admin-Katalogstatus inklusive Backups |
| `POST /api/admin/catalog/validate` | SQLite-Katalogdatei nur validieren |
| `POST /api/admin/catalog/upload` | SQLite-Katalogdatei validieren, Backup anlegen und aktiv ersetzen |
| `POST /api/admin/catalog/regional/import` | regionalen PDF-Katalog importieren |
| `POST /api/admin/catalog/ebm/scrape` | KBV-EBM-Quartal als Hintergrundjob importieren |
| `GET /api/admin/catalog/jobs/{job_id}` | Status eines Katalog- oder Regeljobs abrufen |
| `POST /api/admin/rules/compile` | Katalogregeln kompilieren, nach Supabase migrieren und aktivieren |
| `GET /api/rules` | aktive Regelwerk-Version sowie direkte, zeitabhängige und abgeleitete Regeln |
| `POST /api/documents/analyze` | PDF hochladen und Rechnungsentwurf erzeugen |
| `POST /api/documents/analyze/jobs` | PDF-Analyse als Hintergrundjob starten |
| `GET /api/documents/analyze/jobs/{job_id}` | Status eines Analysejobs abrufen |
| `GET /api/analyses/{analysis_id}` | gespeicherten Analyseentwurf abrufen |
| `GET /api/invoices` | gespeicherte Rechnungsentwürfe listen |
| `GET /api/invoices/{analysis_id}` | gespeicherten Rechnungsentwurf mit Positionen laden |
| `DELETE /api/invoices/{analysis_id}` | gespeicherten Rechnungsentwurf löschen |

## Nächste fachliche Schritte

- echte Zieldefinition für den standardisierten Export festlegen
- Goldstandard-Set aus mehreren Fällen aufbauen
- Review-Regeln für EKG, Konsile, Drogenscreening, Schwangerschaftstest und erweiterte Laborwerte validieren
- serverseitigen Direktimport aus KBV-/Hessen-GOP-Quellen ergänzen
- Sachbearbeiter-Workflow mit Kandidatenfreigabe persistieren
