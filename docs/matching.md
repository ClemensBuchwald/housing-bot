# Matching-Logik

## Ablauf

Jedes neue Inserat durchläuft sequenziell Pflicht-Filter und optionale Scoring-Stufen.

```
Inserat
  │
  ├─ Pflichtfilter (Ausschluss bei Fehler)
  │    ├── Warmmiete ≤ max?
  │    ├── Fläche ≥ min?
  │    ├── Zimmer in [min, max]?
  │    ├── Stadt/Stadtteil nicht in Ausschlussliste?
  │    └── Kein Ausschluss-Schlüsselwort im Titel?
  │
  ├─ Bestanden? → Scoring (0–100 Punkte)
  │    ├── Stadtteil bevorzugt?       +20
  │    ├── Balkon/Terrasse?           +15
  │    ├── Warmmiete weit unter Max?  +10
  │    ├── Haustiere erlaubt?         +10
  │    └── Einzug passt?             +10
  │
  └─ Score ≥ Schwellwert → Benachrichtigung
```

## Pflichtfilter

### Preis
- Warmmiete wird bevorzugt geprüft.
- Falls Warmmiete unbekannt: Kaltmiete gegen `kaltmiete_max`.
- Falls beides unbekannt: Inserat wird **nicht** gefiltert (lieber einmal zu viel melden).

### Fläche
- Nur prüfen wenn `min_qm > 0`.

### Zimmer
- Halbe Zimmer werden aufgerundet (2,5 → 3).

### Ausschluss-Schlüsselwörter
- Prüfung gegen Titel und Beschreibung (falls verfügbar), case-insensitiv.

## Scoring

Scoring ist additiv. Maximaler Score: 100.
Schwellwert für Benachrichtigung: 0 (alle bestandenen Inserate werden gemeldet).
Späterer Ausbau: Priorisierung per Score möglich (z. B. Score > 50 → sofortige Benachrichtigung, Rest in Digest).

## Konfiguration

Alle Parameter kommen aus `config/criteria.yaml`. Die Matching-Engine liest die Datei beim Start und bei explizitem Reload — kein Neustart nötig.

## Erweiterbarkeit

Neue Filter/Scores als eigenständige Funktionen in `src/matching.py` hinzufügen:

```python
def filter_stadtteil(listing: Listing, criteria: Criteria) -> bool: ...
def score_balkon(listing: Listing, criteria: Criteria) -> int: ...
```
