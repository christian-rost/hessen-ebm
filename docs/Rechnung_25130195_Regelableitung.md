# Regelableitung aus Fall 25130195

Diese Notiz beschreibt, welche Regeln sich aus `25130195.pdf` und der dazugehörigen technischen Abrechnungsdatei `Rechnung 25130195.txt` ableiten lassen.

Wichtig: Die PDF ist keine fertige Rechnungstabelle. Sie enthält klinische Dokumentation, Radiologieanforderungen, Radiologiebefunde, Datenerfassung und Laborbefunde. Die GOPs werden daraus über Regeln, Mappings und Abrechnungsfilter erzeugt.

## Ausgangsdaten

| Merkmal | Wert |
| --- | --- |
| Behandlungsdatum | 15.10.2025 |
| Abrechnungsquartal | `2025/Q4` |
| Fallart | Zentrale Notaufnahme / KV-Abrechnung Notfall |
| Abgerechnete GOPs | 15 Positionen |
| Referenzkatalog | KBV-EBM `2025/Q4` |
| Regionaler Treffer Hessen-GOP | keiner |

Die abgerechneten Positionen sind vollständig im bundesweiten EBM `2025/Q4` auffindbar. Der Hessen-GOP-Katalog ergänzt diesen Fall nicht.

## Abgerechnete GOPs mit erkannter Evidenz

| GOP | Bedeutung laut EBM | Evidenz in der PDF | Regelkandidat |
| --- | --- | --- | --- |
| `01210` | Notfallpauschale I | ZNA-/Notfallkontext, KV-Abrechnung Notfall, Behandlung am 15.10.2025 | Einmal je Notfallbehandlungsfall, wenn Fall als KV-Notfall/ZNA klassifiziert ist |
| `32113` | Quick-Wert, Plasma | Laborbefund: Quick im Bereich Gerinnung | Wenn Laborwert `Quick` vorliegt und ambulant abrechenbar ist, erzeuge `32113` |
| `32070` | GPT | Laborbefund: ALT/GPT erhöht | Mappe Laborparameter `ALT`/`GPT` auf `32070` |
| `32083` | Natrium | Laborbefund: Natrium | Mappe Laborparameter `Natrium` auf `32083` |
| `32025` | Glucose | Laborbefund: Glucose | Mappe Laborparameter `Glucose` auf `32025` |
| `32035A` | Basis-GOP `32035`, Erythrozytenzählung | Laborbefund: Erythrozyten | Mappe auf Basis-GOP `32035`; Originalsuffix `A` erhalten |
| `32036A` | Basis-GOP `32036`, Leukozytenzählung | Laborbefund: Leukozyten | Mappe auf Basis-GOP `32036`; Originalsuffix `A` erhalten |
| `32037A` | Basis-GOP `32037`, Thrombozytenzählung | Laborbefund: Thrombozyten | Mappe auf Basis-GOP `32037`; Originalsuffix `A` erhalten |
| `32038A` | Basis-GOP `32038`, Hämoglobin | Laborbefund: Hämoglobin | Mappe auf Basis-GOP `32038`; Originalsuffix `A` erhalten |
| `32039A` | Basis-GOP `32039`, Hämatokrit | Laborbefund: Hämatokrit | Mappe auf Basis-GOP `32039`; Originalsuffix `A` erhalten |
| `32066` | Kreatinin | Laborbefund: Kreatinin | Mappe Laborparameter `Kreatinin` auf `32066` |
| `32081` | Kalium | Laborbefund: Kalium | Mappe Laborparameter `Kalium` auf `32081` |
| `34241` | Übersichtsaufnahme der Brustorgane, zwei Ebenen | Radiologiebefund und Datenerfassung: Röntgen Lunge/Thorax in 2 Ebenen | Wenn Röntgen Lunge/Thorax mit p.a. und lateral/2 Ebenen durchgeführt wurde, erzeuge `34241` |
| `34311` | CT-Untersuchung von Teilen der Wirbelsäule | Radiologiebefund und Datenerfassung: CT CTLWS/LWS | Wenn CT der Wirbelsäule/Teil-Wirbelsäule durchgeführt wurde, erzeuge `34311` |
| `34345` | Zuschlag Kontrastmitteluntersuchung | CT mit `+KM`, KM-Gabe intravenös, Kontrastmittel Imeron | Wenn zu einer CT-Leistung eine Kontrastmittelgabe dokumentiert ist, erzeuge zusätzlich `34345` |

## Wichtige Normalisierungsregeln

### GOP-Suffixe

Die Rechnung enthält Labor-GOPs mit Suffix, z. B. `32035A`. Der EBM-Katalog enthält die Basis-GOP `32035`.

Regel:

```text
gop_original = 32035A
gop_base     = 32035
gop_suffix   = A
```

Für die EBM-Validierung wird gegen `gop_base` gesucht. Für die Rechnung und Reproduzierbarkeit bleibt `gop_original` erhalten.

### Quartal

Das Abrechnungsquartal wird aus dem Behandlungsdatum abgeleitet, nicht aus dem Rechnungsdatum.

```text
15.10.2025 -> 2025/Q4
```

Alle EBM- und regionalen Katalogsuchen müssen mit diesem Quartal erfolgen.

## Abgeleitete Regeltypen

### 1. Kontextregel

Der Fallkontext erzeugt GOPs, die nicht aus einem einzelnen Labor- oder Radiologiebefund stammen.

Beispiel:

```text
Wenn Fallart = KV-Notfall/ZNA
dann Kandidat: 01210 Notfallpauschale I
```

### 2. Laborparameter-Regel

Ein Laborbefund erzeugt nicht automatisch für jeden Laborwert eine GOP. Es braucht ein explizites Mapping zwischen Laborparameter und GOP.

Beispiele:

```text
Quick       -> 32113
Kreatinin   -> 32066
Natrium     -> 32083
Kalium      -> 32081
Glucose     -> 32025
ALT/GPT     -> 32070
```

Für das Blutbild werden einzelne abrechenbare Bestandteile gemappt:

```text
Erythrozyten  -> 32035
Leukozyten    -> 32036
Thrombozyten  -> 32037
Hämoglobin    -> 32038
Hämatokrit    -> 32039
```

### 3. Prozedur-/Leistungsregel Radiologie

Radiologie-GOPs werden aus durchgeführten Untersuchungen, Befundtexten, lokalen Leistungskennungen und ggf. OPS-/Prozedurhinweisen abgeleitet.

Beispiele:

```text
Röntgen Lunge 2 Ebenen / Thorax p.a. + lateral -> 34241
CT CTLWS/LWS/WBS-Teilabschnitt                 -> 34311
CT + KM / intravenöse Kontrastmittelgabe        -> 34345
```

### 4. Zuschlagsregel

Ein Zuschlag entsteht nur im Kontext einer passenden Hauptleistung.

Beispiel:

```text
Wenn CT-Leistung vorhanden
und Kontrastmittelgabe dokumentiert
dann zusätzlich 34345
```

`34345` sollte also nicht isoliert erzeugt werden.

### 5. Ausschluss-/Nichtableitungsregel

Nicht jeder dokumentierte Wert oder interne Auftrag wird abrechenbar.

In diesem Fall wurden nicht abgerechnet, obwohl sie in der PDF vorkommen:

| Dokumentierter Inhalt | Beobachtung |
| --- | --- |
| CRP | Im Laborbefund vorhanden, aber keine GOP in der Rechnung |
| INR/APTT | Im Laborbefund vorhanden, aber keine GOP in der Rechnung |
| Urinstatus | Dokumentiert, aber keine GOP in der Rechnung |
| interne Radiologiecodes wie Dringlichkeitszuschlag, Befunddemonstration, Venenverweilkanüle | In der Radiologie-Datenerfassung vorhanden, aber nicht als EBM-GOP abgerechnet |

Daraus folgt:

```text
PDF-Evidenz allein reicht nicht.
Eine GOP entsteht nur, wenn ein freigegebenes Mapping plus Abrechnungsregel greift.
```

## Empfohlene Datenbankergänzung

Für die spätere Automatisierung sollte zwischen Rohdokument, erkannter Evidenz, Regel und erzeugter Abrechnungsposition unterschieden werden.

Vorgeschlagene Tabellen:

| Tabelle | Zweck |
| --- | --- |
| `clinical_documents` | Importierte Dokumente wie PDF, TXT, Arztbrief, Laborbefund |
| `clinical_evidence` | Extrahierte Hinweise: Laborwert, Radiologieauftrag, Befund, Fallkontext |
| `billing_rule_definitions` | Versionierte Regeln/Mappings, z. B. `Quick -> 32113` |
| `billing_rule_matches` | Welche Regel hat auf welche Evidenz gepasst |
| `billing_candidates` | Vorgeschlagene GOPs vor finaler Prüfung |
| `billing_items` | Final übernommene Rechnungspositionen |

Minimaler Ablauf:

```text
PDF/TXT importieren
  -> Evidenz extrahieren
  -> Evidenz normalisieren
  -> Regelkandidaten erzeugen
  -> gegen EBM/Hessen-GOP des Behandlungsquartals validieren
  -> Ausschlüsse, Häufigkeiten und Kontext prüfen
  -> finale billing_items schreiben
```

## Erste belastbare Regeln aus diesem Fall

Diese Regeln sind für einen Prototypen direkt verwendbar:

1. `KV-Notfall/ZNA` am Behandlungstag erzeugt Kandidat `01210`.
2. `Röntgen Lunge/Thorax 2 Ebenen` erzeugt Kandidat `34241`.
3. `CT CTLWS/LWS/Teil-Wirbelsäule` erzeugt Kandidat `34311`.
4. `CT + KM` oder dokumentierte intravenöse Kontrastmittelgabe erzeugt zusätzlich Kandidat `34345`.
5. `Quick` im Labor erzeugt Kandidat `32113`.
6. `ALT` wird als `GPT` interpretiert und erzeugt Kandidat `32070`.
7. `Kreatinin`, `Natrium`, `Kalium`, `Glucose` erzeugen `32066`, `32083`, `32081`, `32025`.
8. Blutbild-Bestandteile erzeugen die Basis-GOPs `32035` bis `32039`; vorhandene Suffixe bleiben als `gop_original` erhalten.
9. Dokumentierte Befunde ohne freigegebenes Mapping erzeugen keine GOP.
10. Jede erzeugte GOP muss gegen den Katalog des Behandlungsquartals validiert werden.
