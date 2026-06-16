# Admin-Katalogimport

Die Anwendung kann die aktive EBM-/Hessen-GOP-Katalogdatenbank ueber den Admin-Bereich ersetzen.

## Ziel

Die Abrechnungssoftware soll nicht davon abhaengen, dass die Datei `ebm_kbv.sqlite` manuell per SSH oder Dateisystemzugriff in ein Deployment kopiert wird. Ein berechtigter Admin soll eine neue Katalogdatenbank hochladen, pruefen und aktiv schalten koennen.

## Importierter Dateityp

Der aktuelle MVP importiert eine vorbereitete SQLite-Datenbank, also die Datei, die mit den bestehenden Import-/Scraping-Werkzeugen erzeugt wurde.

Typischer Zielpfad:

```text
/app/catalog/ebm_kbv.sqlite
```

Dieser Pfad wird ueber `CATALOG_DB_PATH` konfiguriert.

## Validierung

Vor dem Aktivieren wird die hochgeladene Datei geprueft:

1. Datei existiert und ist nicht leer
2. SQLite laesst sich im Read-only-Modus oeffnen
3. `pragma integrity_check` liefert `ok`
4. Pflichttabellen sind vorhanden:
   - `snapshots`
   - `nodes`
   - `details`
5. Es gibt mindestens einen Snapshot und mindestens eine GOP-Detailzeile
6. Optionale regionale Tabellen wie `regional_catalogs` und `regional_gops` werden erkannt, sind aber fuer reine EBM-Datenbanken nicht zwingend

## Aktivierung

Wenn die Validierung erfolgreich ist:

1. Die bisherige aktive Datenbank wird nach `STORAGE_DIR/catalog-backups` kopiert.
2. Die neue Datenbank wird zunaechst als temporaere Datei neben `CATALOG_DB_PATH` geschrieben.
3. Die temporaere Datei ersetzt die aktive Datenbank atomar per `os.replace`.
4. Veraltete SQLite-Sidecars `-wal` und `-shm` werden entfernt.
5. Die neue aktive Datenbank wird erneut validiert.

## API

| Endpoint | Zweck |
| --- | --- |
| `GET /api/admin/catalog/status` | Aktiver Katalogstatus inklusive Backupliste |
| `POST /api/admin/catalog/validate` | Hochgeladene SQLite-Datei nur pruefen |
| `POST /api/admin/catalog/upload` | Hochgeladene SQLite-Datei pruefen und aktiv ersetzen |

Wenn `ADMIN_TOKEN` gesetzt ist, muessen Admin-Aufrufe den Header `X-Admin-Token` mitsenden.

## Coolify

Empfohlene Volumes:

| Mount | Zweck |
| --- | --- |
| `/app/catalog` | aktive Katalogdatenbank |
| `/app/storage` | Uploads, Analyseartefakte, Katalogbackups |

Die aktuelle Katalogdatenbank ist groesser als 200 MB. Das Frontend-Nginx ist deshalb auf `client_max_body_size 600m` gesetzt. Falls ein vorgelagerter Coolify-/Proxy-Layer kleinere Uploadlimits hat, muss dieser ebenfalls angepasst werden.

## Noch offen

Der MVP importiert fertige SQLite-Dateien. Ein spaeterer Ausbauschritt kann zusaetzlich einen serverseitigen KBV-/Hessen-GOP-Import aus Quellen wie `https://ebm.kbv.de/` und Hessen-GOP-PDFs anbieten.
