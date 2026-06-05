# Quellen-Übersicht

Stand: 2026-06-05 · Zielgebiet: Charlottenburg, Wilmersdorf, Halensee, Grunewald

## Aktive Live-Quellen (im Polling + On-Demand)

| Quelle | Typ | Zugang | Selektoren | Ortsteil |
|--------|-----|--------|-----------|----------|
| **inberlinwohnen.de** | Aggregator (6 Landeseigene) | Server-HTML | `div.results__row` | Adresse+PLZ |
| **degewo** | Landeseigen | Server-HTML (TYPO3/tx_openimmo) | `div.c-teaser--apartment` | Titel/Straße |
| **Heimstaden** | Privat | **JS — Playwright** | diagnose-first | Text/PLZ |
| **WBM** | Landeseigen | Server-HTML | `article.immo-element` | Titel/Adresse |
| **GESOBAU** | Landeseigen | Server-HTML | `article`/Expose | Text |
| **Gewobag** | Landeseigen | Server-HTML | `article.angebot-big-box.gw-offer` | Adresse+PLZ |
| **Vonovia** (inkl. Deutsche Wohnen) | Privat | JSON-API | — | ort+PLZ |
| **Immowelt** | Portal | Server-HTML | `[data-testid=cardmfe-container--test-id]` | Ortsteil+PLZ |
| **Kleinanzeigen** | Portal (privat+Makler) | Server-HTML | `article.aditem` | PLZ |

Hinweis Gewobag: Liste enthält auch Stellplätze/Gewerbe — diese werden gefiltert.
Aktuell (Stand der Prüfung) 0 Wohnungen in CW, aber pollbar für künftige Angebote.

## Bestandsquellen ohne offenen Live-Feed

Diese Genossenschaften haben Bestände im Zielgebiet, aber **keine** maschinell
nutzbare Angebotsseite. Nicht als Scraper gebaut (keine Schein-Quellen).

| Quelle | Bezug | Grund |
|--------|-------|-------|
| **BWV Beamten-Wohnungs-Verein** | Charlottenburg, Wilmersdorf, Schmargendorf | `wohnungsangebote.html` listet serverseitig keine Wohnungen (0 Zimmer/m²) — Freimeldung vermutlich nur intern/Warteliste |
| **Berliner Bau- u. Wohnungsgenossenschaft 1892** | verteilt | `1892.de/wohnungssuche/` ist JS-SPA mit minimalem Server-HTML (1,6 KB) — kein scrapebarer Feed |

→ Beobachtung sinnvoll, aber manuell. Bei Bedarf später per Headless-Browser.

## Technisch (noch) nicht integrierbar

| Quelle | Grund | Möglicher Weg |
|--------|-------|---------------|
| **ImmoScout24** | HTML 401-geblockt; Mobile-API rotiert Endpunkte | offizieller Partner-API-Key |

**Korrektur:** degewo wurde zunächst fälschlich als JS-SPA eingestuft (falscher
URL-Pfad getestet). Tatsächlich ist `immosuche.degewo.de/immosuche` server-seitig
gerendert (TYPO3) → als HTML-Scraper gebaut, **kein Playwright nötig**.

## JS-Quellen via Playwright-Basis (`browser_base.py`)

Wiederverwendbare Headless-Chromium-Basis für echte JS-Quellen:

| Quelle | Status |
|--------|--------|
| **Heimstaden** | erste JS-Quelle, diagnose-first (Selektoren via Server-Logs fixieren) |
| WG-Gesucht | Kandidat für Playwright-Basis (anti-scraping) |
| Engel & Völkers, von Poll | Kandidaten (SPA / Bot-Schutz) |

Infra: Dockerfile installiert Chromium (`playwright install --with-deps chromium`,
~400 MB), compose setzt `shm_size: 1gb`. Lazy-Import: fehlt Playwright, werden
JS-Quellen sauber übersprungen, HTML-Quellen laufen normal weiter.

## Optionaler Sonderkanal (vorgemerkt, nicht gebaut)

- **Wohnen auf Zeit / Zwischenmiete**: bewusst getrennt von Dauerwohnungen.
  Würde als eigener Suchmodus mit eigenem Auftragstyp umgesetzt, nicht in den
  regulären Mietstrom gemischt. Erst nach den regulären Quellen.
