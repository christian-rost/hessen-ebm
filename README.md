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
- Kandidatensuche über den kompletten Quartalskatalog per FTS5-Volltextindex
- semantische LLM-Herleitung von GOPs aus Evidenz und Katalogkandidaten
- Katalogregeln des Quartals als Abrechnungstor
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

## Ableitung: Retrieval, Semantik, Katalogtor

Die Zuordnung Evidenz -> GOP ist nicht konfiguriert, sondern wird pro Fall aus dem Katalog erarbeitet:

```text
Patientenakte
  -> Dokumentsegmente
  -> Zeitleiste und Leistungsereignisse
  -> Evidenzen je Segment
  -> Kandidatensuche im Quartalskatalog (FTS5)
  -> semantische Zuordnung mit Datum, Uhrzeit, Wochentag, Feiertag, Alter, Diagnose
  -> Katalogregeln des Quartals als Abrechnungstor
  -> Rechnungsentwurf mit Sachbearbeiterfreigabe
```

Die Kandidatensuche nutzt den FTS5-Volltextindex der Katalogdatenbank. Das ist wesentlich, weil klinische Dokumentation und EBM-Legende unterschiedlich formulieren: "Röntgen Thorax 2 Ebenen" gegen "Übersichtsaufnahme der Brustorgane, zwei Ebenen". Eine Substring-Suche findet solche Treffer nicht. Gemessen an den zuvor gepflegten Zuordnungen liegt die Trefferquote der Volltextsuche bei 96 Prozent in den Top 25 gegenüber 68 Prozent zuvor; für den ersten Goldstandardfall sind alle 15 GOPs allein über die Katalogsuche erreichbar. Ohne Index fällt die Suche auf `LIKE` zurück.

Das Abrechnungstor sind die kompilierten Katalogregeln des Leistungsquartals, rund 4.500 Regeln mit rund 10.000 Klauseln. Eine vorgeschlagene GOP wird nur dann zur Position, wenn keine maschinell entscheidbare Klausel verletzt ist:

| Klauselergebnis | Wirkung |
| --- | --- |
| Verletzung entscheidbar, z. B. Ausschluss, überschrittene Häufigkeit, Alter außerhalb, Uhrzeit außerhalb, fehlende Voraussetzung | Position entfällt und erscheint als Review-Kandidat mit Begründung |
| nicht entscheidbar, z. B. Genehmigungsvorbehalt, Mindestdauer, patientenbezogene Häufigkeit über den Fall hinaus | Position bleibt, der Klauseltext hängt als Prüfhinweis daran |
| ohne Abrechnungsbezug, z. B. Berichtspflicht | wird ignoriert |

Welche Klauseltypen als Hinweis oder als irrelevant gelten, steht in `clause_policy` in `backend/app/billing_rule_definitions.json`, nicht im Code.

Wichtig für die Einordnung: Das Tor greift nur dort, wo der Katalog eine Bedingung maschinell hergibt. Der Compiler stuft jede Regel als `partial` oder `text_only` ein; eine Stufe "vollständig maschinell geprüft" gibt es bewusst nicht. Prosa-Bedingungen aus Präambeln und Allgemeinen Bestimmungen binden deshalb nicht automatisch. Genau deshalb bleibt jeder Entwurf `draft_needs_human_review`.

Weil die semantische Zuordnung der einzige Weg von Evidenz zu GOP ist, würde ein einzelner Aussetzer des Modells einen leeren Entwurf erzeugen. Eine unbrauchbare Antwort — abgeschnittenes JSON, Prosa statt Objekt, fehlendes `items` — wird deshalb wiederholt; der nächste Versuch bekommt den Fehler ausdrücklich genannt. Die Zahl der Versuche steht in `semantic_policy.max_attempts`, jeder Versuch wird in `catalog_context.billing_derivation.llm_attempts` ausgewiesen.

Ohne `MISTRAL_API_KEY` entstehen keine Rechnungspositionen. Die Analyse liefert dann Segmente, Zeitleiste und Evidenzen, und der Grund steht in `catalog_context.analysis_warnings`.

## Zuschnitt der semantischen Ableitung

Gefragt wird je Leistungsereignis einmal, nicht einmal für den ganzen Fall. Der Server hat Segmente, Sitzungen, Episoden, Kontaktsequenz und Zeitvarianten bereits deterministisch bestimmt; übrig bleibt die eine Frage, für die das Modell nötig ist: **Welcher Katalogeintrag beschreibt diese Leistung?** Eine Wahl aus wenigen Kandidaten statt einer Zuordnung vieler Evidenzen zu vielen GOPs.

Datum, Uhrzeit und Evidenzbezug einer Position stammen aus dem Ereignis, nicht aus der Antwort. Das Modell kann sie deshalb nicht mehr abweichend angeben — eine Fehlerquelle, die zuvor zu Positionen mit falscher Uhrzeit geführt hat.

Der Systemprompt enthält bewusst **keine** Anweisungen zu Sitzungsbildung, Mitternacht, Kontaktsequenz oder Zeitvarianten. Der Server entscheidet das und setzt es anschließend durch; stünde es im Prompt, würde das Modell darüber begründen, ohne es zu bestimmen. Genau daher stammten Begründungen wie „Werktag" an einem Feiertag.

Für einen Beispielfall mit 13 Leistungsereignissen: 13 Aufrufe mit zusammen rund 218.000 Zeichen statt eines Aufrufs mit 341.000, der größte Einzelaufruf rund 22.000 statt 341.000 Zeichen. Die Zahl der Kandidaten je Ereignis steht in `semantic_policy.max_candidates_per_event`.

Zwei Ereignisse zum selben dokumentierten Zeitpunkt können dieselbe GOP wählen — ein CTG und der Untersuchungsbefund derselben Minute. Der Leistungszeitpunkt gehört deshalb in den Dedupe-Schlüssel.

## Zeitstempel und Kontaktsequenzen

Die Leistungszeit wird label-unabhängig erkannt. Klinische Dokumentation beschriftet Zeitstempel beliebig — „Notiz vom", „Vitalwerte vom", „CTG-Streifen vom" —, und eine Liste solcher Beschriftungen wäre nie vollständig. Aufgezählt werden deshalb nur die wenigen **administrativen** Beschriftungen, die kein Leistungszeitpunkt sind: Import-, Export-, Druck-, Scan-, Freigabe- und Validierungszeitpunkte sowie Stammdaten. Alles Übrige gilt als dokumentierter Leistungszeitpunkt. Die Konfiguration steht in `datetime_extraction` in `backend/app/clinical_evidence_definitions.json`.

Zurückgeblickt wird nur bis zum vorherigen Zeitstempel, sonst würde dessen Beschriftung dem nächsten Treffer zugerechnet — bei `Importdatum 13:34 Notiz vom 13:19` sonst beiden.

Notfallkontakte werden über ein Merkmal erkannt, nicht über eine Liste von Evidenzarten: Jede Evidenzregel, deren Metadaten `emergency_contact` setzen, gehört zur Kontaktsequenz. Eine neue Evidenzart mit derselben Bedeutung — etwa eine Fachambulanz statt einer ZNA — wird damit erfasst, ohne dass die Sequenzregel geändert werden muss.

### Sitzungen über die Tagesgrenze

Eine Sitzung wird nach absolutem Zeitabstand gebildet, nicht nach Kalendertag. Beginnt ein Notfallkontakt um 23:40 und wird um 00:30 eine Leistung erbracht, ist das **eine** Sitzung: Der Zeitstempel um 00:30 belegt eine Leistung innerhalb der Sitzung, keinen neuen Kontakt. Eine zweite Basispauschale entsteht daraus nicht, auch wenn 00:30 für sich genommen wieder im Nachtfenster liegt. Zwei unabhängige Prüfungen halten das: eine Basispauschale je Sequenzereignis, und eine GOP-Basis je Leistungsereignis.

### Obligater Leistungsinhalt

Der Katalog nennt bei 1.833 der 3.864 GOPs ausdrücklich, welche Leistung erbracht sein muss. Der Compiler zieht diesen Abschnitt als Klausel `required_service_content` heraus — 4.454 Pflichtelemente je Quartal. Das Modell muss zu jeder vorgeschlagenen GOP angeben, welches Element die Evidenz belegt; der Server prüft, ob die Zuordnung vollständig ist. Verglichen wird über die tragenden Wörter, das Modell darf also kürzen und umstellen.

Damit schließt sich eine Lücke im Abrechnungstor: Geprüft wurden bisher nur Nebenbedingungen — Ausschlüsse, Häufigkeiten, Alter, Uhrzeit —, nie aber, ob die Leistung erbracht wurde, die die GOP beschreibt.

Die Prüfung läuft zunächst im Meldemodus: Eine Lücke hängt als Prüfhinweis an der Position, verhindert sie aber nicht. Scharf geschaltet wird sie über `clause_policy.required_service_content_blocks`. Das ist Absicht — solange nicht gemessen ist, wie zuverlässig das Modell `covered_content` füllt, würde eine harte Sperre korrekte Positionen verwerfen, deren Beleg vorliegt und nur nicht zugeordnet wurde.

### Vorrang der Zeitregel vor unvollständigen Katalogklauseln

Die aus dem Katalogtext kompilierten `time_window`-Klauseln bilden häufig nur die Uhrzeit-Hälfte einer Bedingung ab, nicht die Alternative „oder an Samstagen, Sonntagen, Feiertagen". Hat eine Zeitregel des Regelwerks die Variante bereits aus Datum, Uhrzeit, Wochentag und Feiertag bestimmt, überstimmt eine solche Klausel diese Entscheidung nicht mehr; sie bleibt als Prüfhinweis an der Position. Die Position merkt sich dafür strukturell, welche Zeitregel sie bestimmt hat; eine umbenannte Regel schaltet den Vorrang also nicht stillschweigend ab.

## Mandantenspezifische Leistungskennungen

Klinikinterne Leistungscodes stammen aus dem KIS eines Standorts. Sie sind weder aus dem EBM-Katalog noch aus klinischer Sprache ableitbar und stehen deshalb getrennt in `backend/app/site_service_codes.json`:

| Abschnitt | Inhalt |
| --- | --- |
| `evidence_rules` | vollständige Evidenzregeln für eigene Leistungscodes, z. B. Leistungsbogen-Kürzel |
| `marker_extensions` | zusätzliche Marker für bestehende allgemeine Regeln, etwa hausinterne Radiologiecodes in einer Röntgenregel |
| `candidate_rules` | Zuordnung eigener Evidenzarten zu GOP-Kandidaten |

Beim Laden werden die Standortdefinitionen in das allgemeine Regelwerk eingemischt; die Versionsangabe wird um `+site-<id>-<version>` ergänzt, damit im Regelstatus sichtbar bleibt, welcher Standortstand aktiv ist. Marker werden in jeden passenden Bedingungszweig eingehängt, aber niemals in einen negierten — ein zusätzlicher Marker würde dort die Bedeutung umkehren.

Ein anderer Standort ersetzt ausschließlich diese Datei, per `SITE_DEFINITIONS_PATH`. Fehlt sie, läuft das System ohne Hauscodes weiter; erkannt werden dann nur klinisch formulierte Evidenzen. Ein Architekturtest verhindert, dass Hauscodes in die allgemeinen Regelwerke zurückwandern.

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

- unverbindliche Kandidatenregeln für mehrdeutige Evidenz und interne Leistungskennungen
- zeitabhängige GOP-Gruppen mit beliebig vielen Ergebnisvarianten
- datengesteuerte Sequenzregeln für Erst- und Folgekontakte
- abgeleitete GOPs und Zuschläge mit Voraussetzungen, Kriterien und Ausschlüssen
- Gültigkeitszeiträume nach Quartal sowie regionale Gültigkeit
- Einfügebeziehungen für abgeleitete Rechnungspositionen

Der generische Evaluator unterstützt unter anderem GOP-Voraussetzungen, Evidenzarten, ICD-Präfixe, Volltextmerkmale, Alter, Datum, Uhrzeit, Wochentag, Feiertage, Region, Quartal und strukturierte Metadaten. Weitere Regeln werden als Daten ergänzt; dafür ist keine neue GOP-spezifische Python-Funktion erforderlich. Beide Abrechnungspfade verwenden dasselbe Regelwerk: die deterministische Rechnungserzeugung ebenso wie die Prüfung und Nachbearbeitung der LLM-Vorschläge.

Produktiver Python-Code enthält keine konkreten GOP-Zuordnungen. Direkte Abrechnung, Kandidatenlisten, zeitliche Varianten und Zuschläge werden ausschließlich aus dem versionierten Regelwerk beziehungsweise dem Quartalskatalog geladen. Architekturtests verhindern, dass fachliche Konstanten erneut in `backend/app/**/*.py` eingeführt werden. Geprüft werden GOP-Literale als Text, als vierstellige Kurzform (die der Regelparser auf fünf Stellen auffüllt) und als Zahl, Evidenzart-Literale in den regelausführenden Modulen sowie fest verdrahtete Leistungsquartale.

Auch die Fakten, gegen die Katalogklauseln geprüft werden, sind Daten. Eine kompilierte Klausel vom Typ `requires_<fakt>` wird gegen den Abschnitt `clause_facts` in `backend/app/clinical_evidence_definitions.json` aufgelöst. Ein Fakt gilt als belegt, wenn eine Evidenz das konfigurierte Metadatenflag trägt, ihre Evidenzart gelistet ist oder ein konfigurierter Textmarker vorkommt. Ein neuer Klauseltyp wie `requires_written_report` braucht deshalb nur einen Eintrag in den Definitionen, keine Python-Verzweigung.

Das Leistungsquartal steht nirgends im Code. Es wird aus dem Behandlungsdatum abgeleitet, ersatzweise aus dem Fallkontext übernommen und zuletzt auf den neuesten Snapshot des aktiven Katalogs zurückgeführt. Lässt sich kein Quartal bestimmen, bleibt es leer und die Katalogvalidierung meldet `catalog_missing`, statt still gegen einen festen Katalogstand zu rechnen. `GET /api/catalog/search` verwendet ohne `quarter`-Parameter ebenfalls den neuesten Katalogstand. Der CLI-Importer `hessen_gop_importer.py` verlangt `--quarter` jetzt ausdrücklich, damit ein regionales PDF nicht versehentlich in den falschen Katalogstand importiert wird.

Die zeitliche Regelschicht dedupliziert nicht mehr pauschal fallweit nach GOP. Sie verwendet den Schlüssel aus GOP und Leistungsereignis. Dadurch kann beispielsweise `01786` an zwei verschiedenen Behandlungstagen zweimal vorkommen, während CTG-Start, CTG-Ende, Kurve und Verlaufsnotiz derselben Sitzung nur eine Position erzeugen. Für Kontaktsequenzen gilt zusätzlich: Pro Sequenzereignis entsteht höchstens eine Basispauschale. Eine laufende Notfallsitzung über Mitternacht erzeugt daher keine zweite `01212`; nur ein belegter weiterer Kontakt wird in die passende Folgekonsultations-GOP überführt. Katalogausschlüsse und Häufigkeitsgrenzen werden entsprechend ihrem Geltungsbereich pro Sitzung, Behandlungstag, Behandlungsfall oder Quartal geprüft.

Der Regelcompiler trennt Abrechnungshäufigkeit und Leistungsdauer. Formulierungen wie „einmal im Behandlungsfall“, „einmal im Krankheitsfall“, „je Sitzung“ und „einmal am Behandlungstag“ werden als Häufigkeitsgrenzen mit eigenem Bezugsraum gespeichert. Eine Zeitvorgabe entsteht ausschließlich aus einer ausdrücklichen Legendenformulierung wie „mindestens … Minuten“ oder „je vollendete … Minuten“; letztere wird als Zeitstaffel und nicht als Pauschalen-Häufigkeit modelliert.

Bei `BILLING_RULES_SOURCE=auto` bleiben die aktiven Supabase-Regeln maßgeblich. Neue lokale Kernregeln werden bis zur nächsten Admin-Kompilierung ergänzend eingeblendet, wenn ihre Regel-ID in Supabase noch fehlt. `POST /api/admin/rules/compile` schreibt anschließend das vollständige Kernregelwerk einschließlich der Kandidaten-, Ereignis- und Sequenzregeln nach Supabase.

Das Regelwerk enthält **keine** Zuordnung von Evidenzarten zu GOPs mehr. Welche GOP zu einer Evidenz passt, entscheidet die Kandidatensuche im Quartalskatalog zusammen mit der semantischen Herleitung. Im Regelwerk stehen nur noch Regeln, die aus dem Katalogtext nicht ableitbar sind: Zeitvarianten, Kontaktsequenzen, Zuschlagsbeziehungen und unverbindliche Kandidatenhinweise für mehrdeutige interne Leistungscodes.

`GET /api/rules` liefert die Regelwerk-ID und -Version sowie unverbindliche, zeitabhängige und abgeleitete Regeln.

## API

| Endpoint | Zweck |
| --- | --- |
| `GET /health` | Healthcheck |
| `GET /api/catalog/status` | Katalogstatus |
| `GET /api/catalog/search?q=...` | EBM-/Hessen-GOP-Volltextsuche; ohne `quarter` der neueste Katalogstand |
| `GET /api/admin/catalog/status` | Admin-Katalogstatus inklusive Backups |
| `POST /api/admin/catalog/validate` | SQLite-Katalogdatei nur validieren |
| `POST /api/admin/catalog/upload` | SQLite-Katalogdatei validieren, Backup anlegen und aktiv ersetzen |
| `POST /api/admin/catalog/regional/import` | regionalen PDF-Katalog importieren |
| `POST /api/admin/catalog/ebm/scrape` | KBV-EBM-Quartal als Hintergrundjob importieren |
| `GET /api/admin/catalog/jobs/{job_id}` | Status eines Katalog- oder Regeljobs abrufen |
| `POST /api/admin/rules/compile` | Katalogregeln kompilieren, nach Supabase migrieren und aktivieren |
| `GET /api/rules` | aktive Regelwerk-Version sowie unverbindliche, zeitabhängige und abgeleitete Regeln |
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
