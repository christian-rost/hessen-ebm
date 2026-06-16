# hessen-ebm

MVP fuer eine EBM-/Hessen-GOP-Abrechnungssoftware.

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
- Regel-Engine fuer die validierten GOP-Regeln aus den Faellen `25130195` und `25124444`
- Katalogvalidierung gegen SQLite-EBM/Hessen-GOP
- JSON-Exportprofil `EBM_KVDT_ADT_LIKE_V1_DRAFT`
- Docker-Compose fuer Coolify

## Wichtige Architekturentscheidung

Die EBM-/Hessen-GOP-Katalogdatenbank wird nicht ins Git-Repo gelegt. Die aktuell erzeugte Datei `ebm_kbv.sqlite` ist ca. 225 MB gross und damit fuer ein normales GitHub-Repo ungeeignet.

Stattdessen erwartet die Anwendung den Katalog unter:

```text
CATALOG_DB_PATH=/app/catalog/ebm_kbv.sqlite
```

In Coolify sollte dafuer ein Volume nach `/app/catalog` gemountet werden. Lokal kann `CATALOG_DB_PATH` auch direkt auf die vorhandene SQLite-Datei zeigen.

## Lokale Entwicklung

Backend:

```bash
cd backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
export CATALOG_DB_PATH="/Users/cro/Documents/varisano - ebm Abrechnungsservice/ebm_kbv.sqlite"
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
docker compose -f docker-compose.coolify.yml up --build
```

Danach ist das Frontend lokal unter `http://localhost:8080` erreichbar.

## Coolify Deployment

1. Repository `christian-rost/hessen-ebm` in Coolify verbinden.
2. Compose-Datei `docker-compose.coolify.yml` verwenden.
3. Environment setzen:
   - `CATALOG_DB_PATH=/app/catalog/ebm_kbv.sqlite`
   - `STORAGE_DIR=/app/storage`
   - optional `ENABLE_MISTRAL_OCR=true`
   - optional `MISTRAL_API_KEY=...`
4. Volume fuer `/app/catalog` anlegen und `ebm_kbv.sqlite` dort bereitstellen.
5. Volume fuer `/app/storage` anlegen.

## Aktuell validierte sichere Regeln

| Evidenz | GOP |
| --- | --- |
| KV-Notfall/ZNA | `01210` |
| Quick | `32113` |
| Kreatinin | `32066` |
| Natrium | `32083` |
| Kalium | `32081` |
| Glucose | `32025` |
| ALT/GPT | `32070` |
| Erythrozyten | `32035A` |
| Leukozyten | `32036A` |
| Thrombozyten | `32037A` |
| Haemoglobin | `32038A` |
| Haematokrit | `32039A` |
| Roentgen Thorax/Lunge 2 Ebenen | `34241` |
| CT Wirbelsaeulenabschnitt | `34311` |
| CT mit Kontrastmittel | `34345` |
| CT Kopf nativ | `34310` |
| Roentgen Schulter 2 Ebenen | `34231` |
| Roentgen HWS 2 Ebenen | `34221` |

## API

| Endpoint | Zweck |
| --- | --- |
| `GET /health` | Healthcheck |
| `GET /api/catalog/status` | Katalogstatus |
| `GET /api/catalog/search?q=...&quarter=2025/Q4` | EBM-/Hessen-GOP-Suche |
| `GET /api/rules` | aktuell aktive Regeluebersicht |
| `POST /api/documents/analyze` | PDF hochladen und Rechnungsentwurf erzeugen |
| `GET /api/analyses/{analysis_id}` | gespeicherten Analyseentwurf abrufen |

## Naechste fachliche Schritte

- echte Zieldefinition fuer den standardisierten Export festlegen
- Goldstandard-Set aus mehreren Faellen aufbauen
- Review-Regeln fuer EKG, Konsile, Drogenscreening, Schwangerschaftstest und erweiterte Laborwerte validieren
- Katalogimport in die Admin-Oberflaeche integrieren
- Sachbearbeiter-Workflow mit Kandidatenfreigabe persistieren

