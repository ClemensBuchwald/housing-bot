# Status

*Letzte Aktualisierung: 2026-06-04*

## Phase 1 — Kern

| Aufgabe | Status |
|---------|--------|
| Projektgerüst (Ordner, Docs, Config) | ✅ abgeschlossen |
| Datenmodell (`Listing`, `MatchResult`, `Criteria`) | ✅ abgeschlossen |
| Config-Loader (`criteria.yaml` → Pydantic) | ✅ abgeschlossen |
| SQLite-Deduplizierung (`store.py`) | ✅ abgeschlossen |
| Matching-Engine (Pflichtfilter + Scoring) | ✅ abgeschlossen |
| Telegram-Benachrichtigung | ✅ abgeschlossen |
| Mock-Scraper (Testdaten) | ✅ abgeschlossen |
| IS24-Scraper (Stub, Phase 2) | 🔲 Stub vorhanden |
| Haupt-Run-Loop (`main.py`) | ✅ abgeschlossen |
| pytest-Tests (Matching) | ✅ abgeschlossen |
| Docker Compose Setup | 🔲 offen |
| Deployment auf Server | 🔲 offen |

## Nächste Schritte (Phase 1 abschließen)

1. `.env` lokal befüllen und `python -m src.main --once --mock` testen
2. Telegram-Bot einrichten, Token + Chat-ID in `.env` eintragen
3. Ersten echten Testlauf mit Mock starten
4. Docker Compose schreiben
5. Auf Server deployen

## Phase 2 — Qualität

| Aufgabe | Status |
|---------|--------|
| IS24-Scraper (echte API) | 🔲 offen |
| eBay-Kleinanzeigen-Scraper | 🔲 offen |
| ImmoWelt-Scraper | 🔲 offen |
| Strukturiertes Logging (JSON) | 🔲 offen |
| Scraper-Mocks in Tests | 🔲 offen |
| Healthcheck-Endpoint | 🔲 offen |

## Bekannte Einschränkungen

- IS24-Scraper ist ein Stub — gibt leer zurück mit Warnung im Log.
- Keine echten Portal-Requests in Phase 1 (außer Telegram).
- Docker-Setup noch nicht vorhanden.
