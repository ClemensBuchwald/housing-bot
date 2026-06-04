# Housing Bot

Automatisierter Wohnungs-Suchbot: überwacht Immobilienportale, filtert nach Kriterien und benachrichtigt per Telegram/E-Mail.

## Überblick

- Portale: ImmobilienScout24, ImmoWelt, WG-Gesucht, eBay Kleinanzeigen
- Matching: regelbasiert nach Kriterien aus `config/criteria.yaml`
- Benachrichtigung: Telegram (primär), E-Mail (optional)
- Bewerbung: halbautomatisch (Vorlagen + manuelle Freigabe)
- Deployment: Docker Compose auf eigenem Server

## Projektstruktur

```
housing-bot/
├── docs/               # Architektur, Portale, Matching, Benachrichtigung, Bewerbung, Roadmap
├── config/             # criteria.yaml (Suchkriterien)
├── src/
│   ├── config.py       # Criteria-Modell, YAML-Loader
│   ├── models.py       # Listing, MatchResult
│   ├── matching.py     # Pflichtfilter + Scoring
│   ├── store.py        # SQLite-Deduplizierung
│   ├── notifications.py# Telegram-Versand
│   ├── main.py         # Einstiegspunkt
│   └── scrapers/
│       ├── base.py     # BaseScraper ABC
│       ├── mock.py     # Testdaten
│       └── is24.py     # IS24-Stub (Phase 2)
├── tests/              # pytest-Tests
├── docker/             # Dockerfiles und Compose (Phase 1 Ende)
├── data/               # SQLite-DB (wird automatisch erstellt, nicht ins Git)
├── pyproject.toml      # Abhängigkeiten
├── .env.example        # Vorlage für Umgebungsvariablen
└── CLAUDE.md           # Arbeitsregeln für Claude Code
```

## Lokaler Schnellstart

**Voraussetzungen:** Python 3.12+

```bash
# 1. Abhängigkeiten installieren
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# 2. Umgebungsvariablen setzen
cp .env.example .env
# .env öffnen und TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID eintragen

# 3. Einmalig mit Mock-Daten testen (kein echtes Portal, kein Telegram nötig)
python -m src.main --once --mock

# 4. Mit echten Portalen, im Loop
python -m src.main

# 5. Tests ausführen
pytest
```

**Ohne Telegram:** Bot läuft auch ohne Token — Treffer werden nur ins Log geschrieben.

## Schnellstart (Docker, geplant für Phase 1 Ende)

```bash
cp .env.example .env
# .env befüllen
docker compose up -d
```

## Dokumentation

Alle Konzeptdokumente liegen unter [docs/](docs/).

- [Architektur](docs/architecture.md)
- [Portale](docs/portals.md)
- [Matching-Logik](docs/matching.md)
- [Benachrichtigungen](docs/notifications.md)
- [Bewerbungsprozess](docs/application.md)
- [Roadmap](docs/roadmap.md)
