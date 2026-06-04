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
| Docker Compose Setup | ✅ abgeschlossen |
| Deployment auf Server | 🔲 offen (explizite Freigabe erforderlich) |

## Nächste Schritte (Phase 1 abschließen)

1. `.env` lokal befüllen (`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`)
2. `make run-mock` → Telegram-End-to-End-Test
3. Docker-Image lokal bauen: `make docker-build` (braucht Docker)
4. Docker-Mock-Test: `make docker-mock`
5. Auf Server deployen (nach Freigabe)

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
- Docker-Image wurde noch nicht lokal gebaut (Docker nicht in Entwicklungsumgebung verfügbar).
  Build und Smoke-Test auf einer Maschine mit Docker: `make docker-build && make docker-mock`.
