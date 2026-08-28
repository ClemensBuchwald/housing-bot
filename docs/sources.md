# Quellen-Übersicht

> **Wartungshinweis (2026-08):** Ein Live-Check aller Quellen deckte mehrere
> stille Ausfälle auf — kaputte Pagination (inberlinwohnen, vonovia), falsche
> Orts-IDs (kleinanzeigen suchte ganz Berlin statt CW) und ein Geo-Filter, der
> Bezirksnamen als Ortsteil las. Alle behoben. Lehre: Portale ändern Parameter
> und Formate still; ein Scraper mit HTTP 200 und 0 Treffern ist nicht
> automatisch „korrekt leer". Bei Auffälligkeiten zuerst prüfen, ob die
> Rohtrefferzahl plausibel ist.

Stand: 2026-06-05 · Zielgebiet: Charlottenburg, Wilmersdorf, Halensee, Grunewald

## Aktive Live-Quellen (im Polling + On-Demand)

| Quelle | Typ | Zugang | Selektoren | Ortsteil |
|--------|-----|--------|-----------|----------|
| **inberlinwohnen.de** | Aggregator (6 Landeseigene) | Server-HTML | `div.results__row` | Adresse+PLZ |
| **degewo** | Landeseigen | Server-HTML (TYPO3/tx_openimmo) | `div.c-teaser--apartment` | Titel/Straße |
| **ImmoScout24** | Größtes Portal | **Mobile-API (JSON)** | `/search` + CW-Geocodes | address.line |
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
**Korrektur:** ImmoScout24 wurde zunächst als unzugänglich eingestuft (HTML 401).
Tatsächlich liefert die **Mobile-App-API** (`api.mobile.immobilienscout24.de/search`
mit User-Agent `ImmoScout24_2410_28_._` und CW-Ortsteil-Geocodes) vollständige
Listings als JSON inkl. Expose-Direktlink — als HTML-/API-Scraper gebaut.

**Korrektur:** degewo wurde zunächst fälschlich als JS-SPA eingestuft (falscher
URL-Pfad getestet). Tatsächlich ist `immosuche.degewo.de/immosuche` server-seitig
gerendert (TYPO3) → als HTML-Scraper gebaut, **kein Playwright nötig**.

## Playwright-Basis (`browser_base.py`) — vorhanden, aber AKTUELL keine aktive JS-Quelle

Die wiederverwendbare Headless-Chromium-Basis (inkl. `interact()`-Hook) bleibt im Repo.
Chromium ist im Docker-Image **nicht** installiert (kein aktiver Bedarf); bei echter
JS-Quelle Dockerfile-Zeile + `pip install -e .[browser]` reaktivieren.

**Lokal validiert (2026-06) — beide JS-Kandidaten verworfen:**

| Quelle | Befund | Verdikt |
|--------|--------|---------|
| **Heimstaden** | lädt Listings per **iframe von `portal.immobilienscout24.de`** → ist ein IS24-Widget | **Duplikat von IS24** — deaktiviert |
| **Grand City Property (GCP)** | Filter über Playwright nicht zuverlässig (Cookie-Banner, unsichtbarer Submit), keine Pagination, **0 CW** in zugänglicher Ansicht | fragil + 0 Ausbeute — deaktiviert |

Code (`heimstaden.py`, `gcp.py`) bleibt als Referenz im Repo, ist aber nicht in der
Pipeline aktiv.

**Künftige Kandidaten (falls je nötig):** WG-Gesucht, Engel & Völkers, von Poll —
alle JS/Bot-Schutz, nur über die Playwright-Basis sinnvoll.

## Regionale Quellen geprüft (Phase 4) — bewusst NICHT gebaut

| Quelle | Grund |
|--------|-------|
| **Berlinovo** (landeseigen) | „möbliert / auf Zeit / temporär" → gehört in den separaten **Wohnen-auf-Zeit-Kanal**, nicht in den Dauermiete-Stream |
| **Akelius** | tiefe Angular-SPA, keine offene API, minimale Berlin-Ausbeute → Aufwand/Nutzen schlecht |
| **Berliner Spar- u. Bauverein, WG Charlottenburg-Nord** | Genossenschaften ohne Live-Mietfeed → Bestandsquellen |
| **Covivio** | Domain nicht auflösbar |

## Geprüft 2026-08 — bewusst NICHT gebaut

| Quelle | Befund (live verifiziert) |
|--------|---------------------------|
| **COTRAC** (Hausverwaltung) | Angebotsseite dauerhaft tot: 5× HTTP 500, auch über alle URL-Varianten; `robots.txt` verbietet `/de/page/angebote/` sogar ausdrücklich. Keine maschinenlesbare Liste irgendwo auf der Domain, Kundenportal ist login-only. Ihre Objekte laufen über **IS24** — dort haben wir das Inserat ja gefunden. |
| **DB Wohnen** | Nur Info-Seiten; die Inserate kommen von einem Fremd-Widget (polyestate/AT Estate). Inhaltlich **DB-Mitarbeitenden vorbehalten**. Im Zielgebiet exakt **1** Objekt (Kurfürstendamm 72) — nachweislich identisch mit IS24-Expose 170316860. Der „Charlottenburg"-Hinweis betraf SMARTments = möbliert/auf Zeit (ausgeklammertes Segment). |
| **TAG Wohnen** | Offene REST-API (`immo.isp-10130-1.domservice.de/properties`), technisch trivial scrapebar — aber **0 von 624** Wohnungen in Berlin. Portfolio liegt in Sachsen-Anhalt/Sachsen/Thüringen/Niedersachsen; „Berlin" steht nur mit `doc_count=0` in der Ortsauswahl. Syndiziert zudem an IS24 (49 von 93 Chemnitz-Objekten titelidentisch). Endpunkt dokumentiert, falls TAG je Berliner Bestand übernimmt. |

Gemeinsames Muster: Alle drei enden bei **ImmoScout24**, das wir bereits direkt und
vollständiger abfragen. Einzelne Hausverwaltungen und Konzern-Wohnportale sind für
dieses Zielgebiet praktisch immer Duplikate.

## Optionaler Sonderkanal (vorgemerkt, nicht gebaut)

- **Wohnen auf Zeit / Zwischenmiete**: bewusst getrennt von Dauerwohnungen.
  Würde als eigener Suchmodus mit eigenem Auftragstyp umgesetzt, nicht in den
  regulären Mietstrom gemischt. Erst nach den regulären Quellen.

## Detaildaten & Kontaktrecherche (`src/scrapers/is24_detail.py`)

`GET https://api.mobile.immobilienscout24.de/expose/{id}` (UA `ImmoScout24_2410_28_._`)
liefert je Inserat:

| Feld | Herkunft | Nutzen |
|------|----------|--------|
| Etage, Balkon/Terrasse, Keller, Aufzug, Haustiere | `sections[].ATTRIBUTE_LIST` (`CHECK` = vorhanden) | KI kann „kein Erdgeschoss", „Balkon" endlich wirklich prüfen |
| Objektbeschreibung / Lage | `sections[].TEXT_AREA` | Textverständnis für die Bewertung |
| **Warmmiete** | Attribut „Gesamtmiete" | Die Trefferliste liefert nur Kaltmiete — ohne das ist eine Warmmieten-Obergrenze nicht prüfbar |
| Ansprechpartner, Firma, Kontaktweg | `contact.contactData.agent` | Agent-Tool `kontaktdaten_recherchieren` |

Angereichert wird **nach** der Dedup (nur für wirklich neue Inserate) und vor der
KI-Bewertung. Kontaktdaten: ausschließlich die im Inserat öffentlich sichtbaren
gewerblichen Anbieterdaten — keine Logins, keine privaten Daten Dritter.

## Flutschutz

`MAX_EVAL_PRO_ZYKLUS` (40) und `MAX_ALERTS_PRO_ZYKLUS` (8), per `.env` änderbar.
Verhindert, dass eine neu angebundene Quelle mit leerer Dedup den Chat mit
hunderten Nachrichten flutet. Nicht gemeldete Treffer bleiben gespeichert und
sind über „zeig mir alle" abrufbar.
