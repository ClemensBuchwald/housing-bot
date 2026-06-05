# Quellen-Übersicht

Stand: 2026-06-05 · Zielgebiet: Charlottenburg, Wilmersdorf, Halensee, Grunewald

## Aktive Live-Quellen (im Polling + On-Demand)

| Quelle | Typ | Zugang | Selektoren | Ortsteil |
|--------|-----|--------|-----------|----------|
| **inberlinwohnen.de** | Aggregator (6 Landeseigene) | Server-HTML | `div.results__row` | Adresse+PLZ |
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
| **degewo** (immosuche.degewo.de) | JS-SPA (Immomio), Catch-all-Routing, kein offener API-Pfad | Headless-Browser (Playwright) — separater größerer Schritt |
| **ImmoScout24** | HTML 401-geblockt; Mobile-API rotiert Endpunkte | offizieller Partner-API-Key |

## Optionaler Sonderkanal (vorgemerkt, nicht gebaut)

- **Wohnen auf Zeit / Zwischenmiete**: bewusst getrennt von Dauerwohnungen.
  Würde als eigener Suchmodus mit eigenem Auftragstyp umgesetzt, nicht in den
  regulären Mietstrom gemischt. Erst nach den regulären Quellen.
