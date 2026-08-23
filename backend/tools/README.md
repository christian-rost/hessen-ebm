# Messstand

`measure_cases.py` prüft Rechnungsentwürfe gegen freigegebene Rechnungen und macht
zwei Stände vergleichbar. Er entsteht vor einem Umbau, nicht als dessen Nebenprodukt.

## Fallverzeichnis

Liegt **außerhalb** des Repositories, weil es klinische Akten enthält:

```text
faelle/
  <fallname>/
    akte.pdf
    erwartet.json
```

```json
{
  "quartal": "2026/Q1",
  "region": "Hessen",
  "betrag": 95.80,
  "positionen": [
    {"gop": "01212", "datum": "2026-01-01"},
    {"gop": "01786", "datum": "2026-01-01"}
  ]
}
```

## Aufruf

```bash
cd backend
python -m tools.measure_cases --faelle ../../faelle --katalog /pfad/ebm_kbv.sqlite
python -m tools.measure_cases --faelle ../../faelle --bericht stand.json
python -m tools.measure_cases --faelle ../../faelle --vergleich stand.json
```

Der Bericht enthält GOPs, Leistungsdaten und Kennzahlen, keine Patientendaten.
Weil Leistungsdatum und GOP zusammen einen Fall eingrenzen können, gehört auch er
nicht ins Repository.

## Ohne Modellzugang

Fehlt `MISTRAL_API_KEY`, schlägt die semantische Herleitung fehl. Der Messstand
bricht dann nicht ab, sondern weist den Fall als nicht abgeleitet mit Grund aus und
zählt seine Sollpositionen weiterhin als verfehlt. Die Trefferquote wird dadurch
nicht geschönt.

## Kennzahl

Maßgeblich ist die Trefferquote: der Anteil der Sollpositionen, der ohne Korrektur
entsteht. Zusätzliche Positionen werden getrennt ausgewiesen, weil sie einen anderen
Fehler bedeuten als fehlende.
