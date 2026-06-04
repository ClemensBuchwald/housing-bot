# Portale

## Übersicht

| Portal | Zugang | Rate-Limit-Risiko | Status |
|--------|--------|-------------------|--------|
| ImmobilienScout24 | API (Key) | gering | geplant |
| ImmoWelt | Scraping | mittel | geplant |
| eBay Kleinanzeigen | Scraping | hoch | geplant |
| WG-Gesucht | Scraping | sehr hoch | optional |

---

## ImmobilienScout24 (IS24)

**Zugang:** Offizielle Partner-API mit API-Key.

**Endpunkt:** `https://rest.immobilienscout24.de/restapi/api/search/v3.0/`

**Parameter:**
- `realestatetype=apartmentrent`
- `geocodes` für Stadtteile
- Preis, Fläche, Zimmer als Query-Parameter

**Besonderheiten:**
- Pagination über `pagenumber`
- API-Key in `Authorization: Bearer`-Header
- Antwort: JSON mit `resultlist.resultlistEntries`

**Fallback:** Falls kein API-Key vorhanden, HTML-Scraping als Fallback (höheres Risiko).

---

## ImmoWelt

**Zugang:** Scraping der Suchergebnisseiten.

**URL-Schema:**
```
https://www.immowelt.de/suche/{stadt}/wohnungen/mieten?ami={min_qm}&bmi={max_kaltmiete}
```

**Selektoren (Stand: 2025):**
- Inserat-Container: `[data-testid="serp-core-classified-card"]`
- Preis: `.price-information`
- Fläche/Zimmer: `.facts`

**Rate-Limit:** 1 Request alle 5–15 Sekunden, User-Agent rotieren.

---

## eBay Kleinanzeigen

**Zugang:** Scraping. Kein öffentliches API.

**URL-Schema:**
```
https://www.kleinanzeigen.de/s-wohnung-mieten/{stadt}/c203
```

**Selektoren:**
- Liste: `#srchrslt-adtable article`
- Preis: `.aditem-main--top--right`
- Titel: `h2.text-module-begin`

**Besonderheiten:**
- Session-Cookies nötig (Playwright empfohlen)
- Sehr aggressive Anti-Bot-Maßnahmen → vorsichtiges Polling

---

## WG-Gesucht

**Zugang:** Scraping nach Login.

**Besonderheiten:**
- Login per Session-Cookie
- Captcha möglich
- Nur aktivieren wenn explizit beauftragt

---

## Gemeinsame Scraper-Schnittstelle

Jeder Scraper implementiert:

```python
class BaseScraper:
    def fetch_listings(self, criteria: Criteria) -> list[Listing]: ...
```

Einheitliche Fehlerbehandlung: bei HTTP 429 → exponentielles Backoff, max. 3 Versuche.
