# Validierung und Rechnungsentwurf Fall 25124444

Diese Notiz validiert die bisher aus Fall `25130195` abgeleiteten Regeln an einem zweiten Evidenz-PDF: `/Users/cro/Downloads/LENUS_varisano_ebm/25124444.pdf`.

Der Entwurf ist keine finale Abrechnungsfreigabe. Er zeigt, welche Positionen aus der vorliegenden klinischen Evidenz und den aktuell geladenen Katalogen reproduzierbar ableitbar sind.

## Ausgangsdaten

| Merkmal | Wert |
| --- | --- |
| Quelldokument | `25124444.pdf` |
| Seiten | `54` |
| PDF-Erzeugung | HYDMedia PDF, erstellt am 11.06.2026 |
| Behandlungszeitraum laut Evidenz | 03.10.2025 22:59 bis 04.10.2025 01:19 |
| Abrechnungsquartal | `2025/Q4` |
| EBM-Snapshot | KBV-EBM `2025/Q4`, `3.850` Details |
| Regionaler Katalog | KV Hessen GOP `2025/Q4`, Stand 30.09.2025 |
| Hessen-GOP-Treffer fuer sichere Kandidaten | keine |

## Relevante Dokumentsegmente

| Seiten | Segment | Relevanz fuer Abrechnung | Beobachtung |
| --- | --- | --- | --- |
| 1-4 | Radiologieanforderung / Indikationspruefung | niedrig bis mittel | Auftrag allein reicht nicht; Durchfuehrung muss separat belegt sein. |
| 5 | ZNA-Triage / Fallkontext | hoch | `KV-Abrechnung/Notfalldienst/Vertretung/Notfall`, ZNA-Kontext, Aufnahme 03.10.2025 22:59. |
| 19-21 | Behandlungsbericht ZNA | hoch | Diagnose `S00.95`, Diagnostik und Verlauf, Behandlung endet 04.10.2025 01:19. |
| 22-25 | Neurologisches und psychiatrisches Konsil | mittel | Klinisch relevant, aber noch kein freigegebenes EBM-/Hessen-GOP-Mapping. |
| 31-36 | EKG | niedrig bis mittel | 12-Kanal-EKG sichtbar, aber noch kein belastbarer EBM-Kandidat aus dem aktuellen Regelset. |
| 37-38 | Radiologiebefunde | hoch | CT Kopf nativ, Roentgen Schulter links, Roentgen HWS jeweils mit Durchfuehrungszeit. |
| 41-45 | Datenerfassung Konsil/Radiologie | mittel | Nuetzlich als Zusatzbeleg; interne Codes/Zuschlaege nicht automatisch abrechenbar. |
| 48-53 | Laborbefunde | hoch | Abrechenbare Laborparameter und Ausschluesse, z. B. unterfuellte Gerinnungsprobe. |

## Validierung der bisherigen Regeln

Die aus Fall `25130195` abgeleiteten Regeln tragen auch hier:

| Regel | Ergebnis im neuen Fall |
| --- | --- |
| KV-Notfall/ZNA erzeugt Kandidat `01210` | bestaetigt durch Seite 5 und Behandlungsbericht |
| Laborwerte werden nur ueber freigegebene Parameter-Mappings abgerechnet | bestaetigt; viele sichtbare Werte werden bewusst nicht automatisch uebernommen |
| Blutbildbestandteile koennen als Basis-GOPs `32035` bis `32039` validiert werden | bestaetigt; Suffix `A` bleibt im Rechnungsentwurf erhalten |
| GOP-Suffixe werden fuer die Validierung auf die Basis-GOP normalisiert | bestaetigt |
| Katalogsuche erfolgt ueber das Leistungsquartal | bestaetigt: 03./04.10.2025 -> `2025/Q4` |
| Regionale GOPs werden nur bei Treffer im passenden regionalen Katalog genutzt | bestaetigt; fuer die sicheren Kandidaten kein Hessen-GOP-Treffer |

## Neue beziehungsweise geschaerfte Regeln

Dieser Fall erweitert die Radiologie-Regeln:

| Evidenz | GOP-Kandidat | Regel |
| --- | --- | --- |
| CT Kopf nativ, durchgefuehrt am 04.10.2025 00:03, Seite 37 | `34310` | CT des Neurocraniums erzeugt `34310`. |
| Roentgen Schulter 2 Ebenen links, durchgefuehrt am 04.10.2025 00:01, Seite 38 | `34231` | Roentgen Schulter/Schulterguertel erzeugt `34231`. |
| Roentgen HWS 2 Ebenen ap/seitlich/Dens, durchgefuehrt am 04.10.2025 00:01, Seite 38 | `34221` | Roentgen eines Wirbelsaeulenabschnitts erzeugt `34221`. |

Wichtige Praezisierung: Eine angeforderte oder in der Datenerfassung auftauchende Leistung darf nicht automatisch abgerechnet werden. Bei Widerspruch ist der signierte Befund mit Durchfuehrungszeit staerker als ein Auftrag oder eine Storno-Zeile.

Konkret hier:

| Inhalt | Entscheidung | Grund |
| --- | --- | --- |
| CT HWS nativ | nicht uebernommen | nur angefordert/storniert, kein Radiologiebefund als durchgefuehrt gefunden |
| CT-Kontrastmittelzuschlag `34345` | nicht uebernommen | CT Kopf ist als nativ dokumentiert, keine KM-Gabe |
| interner Radiologiecode `RAS9048` | nicht uebernommen | lokaler Zuschlag ohne freigegebenes EBM-/Hessen-GOP-Mapping |
| Quick/INR/APTT aus erster Probe | nicht uebernommen | Gerinnungsprobe Seite 48 als unterfuellt/falsches Mischungsverhaeltnis markiert |
| Quick aus zweiter Probe | uebernommen | Seite 52 zeigt validen Quick-Wert |
| Konsile Neurologie/Psychiatrie | Review-Kandidaten | klinisch belegt, aber noch keine freigegebene Abrechnungsregel |
| Drogen-Screening, Schwangerschaftstest, CRP, Urinstatus, weitere Enzyme | Review-Kandidaten | Katalogtreffer teilweise vorhanden, aber noch keine Regel aus Goldstandard validiert |

## Sicherer Rechnungsentwurf

| Pos. | GOP original | GOP basis | Bedeutung | Datum | Zeit | Evidenz | Punkte | EUR |
| ---: | --- | --- | --- | --- | --- | --- | ---: | ---: |
| 1 | `01210` | `01210` | Notfallpauschale I | 03.10.2025 | 22:59 | ZNA/KV-Notfall, Seiten 5 und 19 | 120 | 14.87 |
| 2 | `34310` | `34310` | CT-Untersuchung des Neurocraniums | 04.10.2025 | 00:03 | CT Kopf nativ, Seite 37 | 534 | 66.18 |
| 3 | `34231` | `34231` | Aufnahmen der Schulter/des Schulterguertels | 04.10.2025 | 00:01 | Roentgen Schulter 2 Ebenen links, Seite 38 | 137 | 16.98 |
| 4 | `34221` | `34221` | Aufnahmen von Teilen der Wirbelsaeule | 04.10.2025 | 00:01 | Roentgen HWS 2 Ebenen, Seite 38 | 140 | 17.35 |
| 5 | `32113` | `32113` | Quick-Wert, Plasma | 04.10.2025 | 00:19 | valide Gerinnung, Seite 52 | 0 | 0.58 |
| 6 | `32066` | `32066` | Kreatinin | 04.10.2025 | 00:19 | Laborbefund, Seite 52 | 0 | 0.25 |
| 7 | `32083` | `32083` | Natrium | 04.10.2025 | 00:19 | Laborbefund, Seite 52 | 0 | 0.25 |
| 8 | `32081` | `32081` | Kalium | 04.10.2025 | 00:19 | Laborbefund, Seite 52 | 0 | 0.25 |
| 9 | `32025` | `32025` | Glucose | 04.10.2025 | 00:19 | Laborbefund, Seite 52 | 0 | 1.60 |
| 10 | `32070` | `32070` | GPT / ALT | 03.10.2025 | 23:20 | ALT, Seite 48 | 0 | 0.25 |
| 11 | `32035A` | `32035` | Erythrozytenzaehlung | 04.10.2025 | 00:19 | Blutbild, Seiten 52-53 | 0 | 0.25 |
| 12 | `32036A` | `32036` | Leukozytenzaehlung | 04.10.2025 | 00:19 | Blutbild, Seiten 52-53 | 0 | 0.25 |
| 13 | `32037A` | `32037` | Thrombozytenzaehlung | 04.10.2025 | 00:19 | Blutbild, Seiten 52-53 | 0 | 0.25 |
| 14 | `32038A` | `32038` | Haemoglobin | 04.10.2025 | 00:19 | Blutbild, Seiten 52-53 | 0 | 0.25 |
| 15 | `32039A` | `32039` | Haematokrit | 04.10.2025 | 00:19 | Blutbild, Seiten 52-53 | 0 | 0.25 |

Summe:

| Kennzahl | Wert |
| --- | ---: |
| Positionen | 15 |
| Punkte | 931 |
| Katalogwert | 119.81 EUR |

Hinweis zur Labor-Deduplizierung: Mehrere Laborauftraege im selben Behandlungsfall duerfen nicht blind mehrfach abgerechnet werden. Der Entwurf uebernimmt fuer doppelt vorhandene Basisparameter nur eine Position und bevorzugt den fertigen/validen Befund.

## Review-Kandidaten fuer spaetere Regelarbeit

| Evidenz | Moeglicher Katalogbezug | Warum noch nicht automatisch abrechnen? |
| --- | --- | --- |
| EKG, Seiten 31-36 | noch kein sicherer GOP-Treffer im aktuellen Mapping | braucht fachliche EBM-Zuordnung und Kontextregel |
| Neurologisches Konsil, Seiten 22 und 41 | Konsil-/Bereitschaftsdienst moeglich | interne Konsiltypen sind nicht gleich EBM-GOP |
| Psychiatrisches Konsil, Seiten 24 und 42 | Konsil-/Bereitschaftsdienst moeglich | interne Konsiltypen sind nicht gleich EBM-GOP |
| Schwangerschaftstest Urin, Seite 51 | `32132` existiert im EBM | im Radiologiebefund steht zugleich, dass auf Schwangerschaftstest verzichtet wurde; Laborbeleg und klinischer Kontext muessen geklaert werden |
| Drogen-Screening Urin, Seite 50 | Drogen-GOPs existieren im EBM | Panel-/Einzeltestlogik und Abrechnungsfaehigkeit nicht validiert |
| CRP, CK, CK-MB, Myoglobin, Harnstoff, GOT/AST, Gamma-GT | EBM-Treffer teilweise vorhanden | bisherige Goldstandard-Regel erlaubt nicht automatisch jeden Laborwert |
| Urinstatus, Seite 51 | `32720` existiert im EBM | im ersten Fall als nicht abgerechnet beobachtet; braucht explizite Positivregel |

## Ergebnis der Validierung

Der zweite Fall bestaetigt die Grundarchitektur:

1. Dokumentsegmentierung ist notwendig, weil Auftrag, Befund, Datenerfassung und Labor unterschiedliche Beweiskraft haben.
2. Die Rechnung entsteht aus freigegebenen Regeln, nicht aus einer Volltextsuche nach allen Katalogtreffern.
3. Die Katalogversion muss aus dem Leistungsdatum bestimmt werden.
4. EBM und Hessen-GOP bleiben getrennte Referenzschichten; in diesem Fall greift nur der bundesweite EBM.
5. Die Software braucht einen Review-Workflow fuer sichtbare, aber noch nicht freigegebene Evidenz.

Der maschinenlesbare Entwurf liegt in `Rechnung_25124444_Standardexport_Entwurf.json`.
