# Housing Bot

Automatisierter Wohnungs-Suchbot: überwacht Immobilienportale, filtert nach Kriterien und benachrichtigt per Telegram.

## Überblick

- Portale: ImmobilienScout24, ImmoWelt, WG-Gesucht, eBay Kleinanzeigen
- Matching: regelbasiert nach Kriterien aus `config/criteria.yaml`
- Benachrichtigung: Telegram (primär), E-Mail (optional)
- Bewerbung: halbautomatisch (Vorlagen + manuelle Freigabe, Phase 2)
- Deployment: Docker Compose auf eigenem Server

## Projektstruktur

```
housing-bot/
├── docs/               # Architektur, Portale, Matching, Benachrichtigung, Bewerbung, Roadmap
├── config/
│   └── criteria.yaml   # Suchkriterien (Preis, Fläche, Zimmer, Stadtteile …)
├── src/
│   ├── config.py       # Criteria-Modell, YAML-Loader
│   ├── models.py       # Listing, MatchResult
│   ├── matching.py     # Pflichtfilter + Scoring
│   ├── store.py        # SQLite-Deduplizierung
│   ├── notifications.py# Telegram-Versand
│   ├── main.py         # Einstiegspunkt
│   └── scrapers/
│       ├── base.py     # BaseScraper ABC
│       ├── mock.py     # Testdaten (5 Inserate)
│       └── is24.py     # IS24-Stub (Phase 2)
├── tests/              # pytest-Tests
├── docker/
│   └── Dockerfile
├── compose.yaml        # Docker Compose (lokal + Server)
├── Makefile            # Kurzkommandos
├── data/               # SQLite-DB (automatisch erstellt, nicht im Git)
├── pyproject.toml      # Abhängigkeiten
├── .env.example        # Vorlage für Umgebungsvariablen
└── CLAUDE.md           # Arbeitsregeln für Claude Code
```

---

## Lokaler Start (Python)

**Voraussetzungen:** Python 3.9+

```bash
# 1. Abhängigkeiten installieren
make install
# oder manuell:
python -m venv .venv && source .venv/bin/activate
pip install pydantic pyyaml httpx python-dotenv

# 2. Umgebungsvariablen setzen
cp .env.example .env
# → .env öffnen, TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID eintragen
# → Anleitung: docs/telegram-setup.md

# 3. Ohne Telegram: Smoke-Test mit Mock-Daten
make run-mock
# Erwartet: 3 Treffer im Log (Prenzlauer Berg, Friedrichshain, Neukölln)

# 4. Mit Telegram: End-to-End-Test
# .env befüllen, dann:
make run-mock
# → 3 Telegram-Nachrichten sollten ankommen

# 5. Dauerbetrieb (alle 10 Minuten)
make run

# 6. Tests
make test
```

**Ohne Token:** Bot läuft ohne Absturz — Treffer werden nur ins Terminal geloggt.

---

## Lokaler Start (Docker)

**Voraussetzungen:** Docker + Docker Compose (V2)

```bash
# 1. Image bauen
make docker-build

# 2. Umgebungsvariablen setzen (falls noch nicht geschehen)
cp .env.example .env
# → .env befüllen

# 3. Einmalig mit Mock-Daten testen
make docker-mock

# 4. Dauerbetrieb starten
make docker-run

# 5. Logs verfolgen
make docker-logs

# 6. Stoppen
make docker-stop
```

Die SQLite-Datenbank liegt in `./data/` (im Repo, außerhalb des Containers).
Die `config/criteria.yaml` wird read-only in den Container gemountet — Änderungen wirken sofort ohne Rebuild.

---

## Telegram einrichten

→ Vollständige Anleitung: [docs/telegram-setup.md](docs/telegram-setup.md)

Kurzfassung:
1. [@BotFather](https://t.me/BotFather) → `/newbot` → Token kopieren
2. Bot in Telegram anschreiben → `https://api.telegram.org/bot<TOKEN>/getUpdates` → Chat-ID ablesen
3. Beides in `.env` eintragen

---

## Suchkriterien anpassen

Alle Suchparameter (Preis, Fläche, Zimmer, Stadtteile, Ausstattung) in `config/criteria.yaml` editieren.
Änderungen werden beim nächsten Polling-Zyklus automatisch geladen — kein Neustart nötig.

---

## Dokumentation

- [Architektur](docs/architecture.md)
- [Portale](docs/portals.md)
- [Matching-Logik](docs/matching.md)
- [Benachrichtigungen](docs/notifications.md)
- [Bewerbungsprozess](docs/application.md)
- [Telegram-Setup](docs/telegram-setup.md)
- [Status & Roadmap](docs/STATUS.md)
- [Roadmap](docs/roadmap.md)
