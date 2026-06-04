# Architektur

## Überblick

Der Housing Bot ist ein Python-Dienst, der im Polling-Betrieb Immobilienportale überwacht, neue Inserate gegen Kriterien prüft und Benachrichtigungen auslöst.

```
┌─────────────────────────────────────────────────────────┐
│                      Scheduler                          │
│              (APScheduler / einfache Loop)              │
└────────────────────┬────────────────────────────────────┘
                     │ alle N Minuten
         ┌───────────▼───────────┐
         │     Portal-Scraper    │
         │  IS24 | ImmoWelt      │
         │  eBay | WG-Gesucht    │
         └───────────┬───────────┘
                     │ rohe Inserate
         ┌───────────▼───────────┐
         │   Deduplizierung      │
         │   (lokale SQLite-DB)  │
         └───────────┬───────────┘
                     │ nur neue Inserate
         ┌───────────▼───────────┐
         │   Matching-Engine     │
         │   (criteria.yaml)     │
         └───────────┬───────────┘
                     │ passende Inserate
         ┌───────────▼───────────┐
         │  Benachrichtigung     │
         │  Telegram / E-Mail    │
         └───────────────────────┘
```

## Komponenten

### Scheduler
Steuert den Polling-Takt (Standard: alle 10 Minuten). Konfigurierbar über `POLL_INTERVAL` in `.env`.

### Portal-Scraper
Für jedes Portal ein eigenes Modul unter `src/scrapers/`. Jeder Scraper liefert eine einheitliche Liste von `Listing`-Objekten.

### Deduplizierung
SQLite-Datenbank (`data/housing_bot.db`) speichert bereits gesehene Inserat-IDs. Verhindert Doppelbenachrichtigungen.

### Matching-Engine
Prüft jedes neue Inserat gegen `config/criteria.yaml`. Liefert `True/False` sowie einen optionalen Score für Priorisierung.

### Benachrichtigung
Sendet formatierte Nachrichten an Telegram und/oder E-Mail. Nachrichtenvorlage enthält: Titel, Preis, Fläche, Zimmer, Link, Stadtteil.

## Datenmodell

```python
@dataclass
class Listing:
    id: str              # Portal-interne ID
    portal: str          # "is24" | "immowelt" | "ebay" | "wg_gesucht"
    url: str
    titel: str
    kaltmiete: float | None
    warmmiete: float | None
    flaeche: float | None
    zimmer: float | None
    stadtteil: str | None
    stadt: str
    verfuegbar_ab: date | None
    merkmale: list[str]  # Freitext-Tags aus dem Inserat
    gefunden_am: datetime
```

## Deployment

Docker Compose auf dem Server unter `/srv/housing-bot/`. Zwei Services:
- `bot`: der Python-Prozess
- (optional) `db`: Postgres, falls SQLite nicht ausreicht

Siehe [docker/](../docker/) für Konfiguration.
