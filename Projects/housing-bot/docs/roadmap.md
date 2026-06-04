# Roadmap

## Phase 1 — Kern (aktuell)

Ziel: Bot läuft stabil, meldet neue Inserate per Telegram.

- [ ] Projektgerüst (Ordner, Docs, Config) ✓
- [ ] Datenmodell (`Listing`, `Criteria`)
- [ ] SQLite-Datenbank (Schema, Migrationen)
- [ ] Scraper: ImmobilienScout24 (API)
- [ ] Scraper: eBay Kleinanzeigen (Scraping)
- [ ] Matching-Engine (Pflichtfilter)
- [ ] Telegram-Benachrichtigung (Basis)
- [ ] Scheduler / Polling-Loop
- [ ] Docker Compose Setup
- [ ] Deployment auf Server

## Phase 2 — Qualität

- [ ] Scraper: ImmoWelt
- [ ] Scoring-System
- [ ] Exponentielles Backoff & Retry-Logik
- [ ] Logging strukturiert (JSON)
- [ ] Tests (pytest, Scraper-Mocks)
- [ ] Healthcheck-Endpoint

## Phase 3 — Bewerbung (halbautomatisch)

- [ ] Telegram Inline-Buttons ("Interessiert" / "Überspringen")
- [ ] Bewerbungsvorlage mit Platzhaltern
- [ ] Versand per E-Mail (IS24, ImmoWelt)
- [ ] Versand per Playwright (eBay, WG-Gesucht)
- [ ] Bewerbungs-Tracking in DB
- [ ] `/status`-Befehl in Telegram

## Phase 4 — Optional / Zukunft

- [ ] WG-Gesucht Scraper
- [ ] Digest-Modus (tägliche Zusammenfassung)
- [ ] Web-UI für Kriterienanpassung
- [ ] Mehrere Suchprofile gleichzeitig
- [ ] Preishistorie / Marktübersicht

---

*Letzte Aktualisierung: 2026-06-04*
