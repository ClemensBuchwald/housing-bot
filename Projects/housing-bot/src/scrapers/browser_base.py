"""Gemeinsame Playwright-/Headless-Chromium-Basis für JS-gestützte Quellen.

Diese Klasse kapselt das Rendern JS-lastiger Seiten, damit konkrete JS-Quellen
(Heimstaden, WG-Gesucht, …) nur noch Selektoren + Parsing liefern müssen.

Design-Prinzipien:
  - Chromium-only, headless, containerstabile Startflags
  - Lazy Import von Playwright: fehlt es, wird die Quelle sauber übersprungen
    (die httpx-basierten HTML-Scraper bleiben davon unberührt)
  - robuste Timeouts + Retry pro URL
  - strukturiertes Logging je Quelle:
      render_ok, listings_found, links_extracted
  - Ergebnis fügt sich in dieselbe Normalisierungs-/Dedupe-/Matching-Pipeline ein
    (gibt List[Listing] zurück wie jeder andere Scraper)

Subklassen implementieren:
  - start_urls() -> List[str]
  - wait_selector: CSS-Selektor, auf den nach dem Laden gewartet wird
  - parse(page_html, url) -> List[Listing]
"""
from __future__ import annotations

import logging
from abc import abstractmethod
from typing import List, Optional, Tuple

from src.config import Criteria
from src.models import Listing
from src.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# Containerstabile Chromium-Flags (kein /dev/shm-Engpass, kein Sandbox-Problem als root)
_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--disable-extensions",
    "--no-first-run",
]


class BrowserScraper(BaseScraper):
    name = "browser-base"

    # Von Subklassen zu setzen:
    wait_selector: Optional[str] = None   # auf dieses Element warten (= Listings da)
    nav_timeout_ms: int = 30000
    wait_timeout_ms: int = 15000
    retries: int = 2

    @abstractmethod
    def start_urls(self) -> List[str]:
        ...

    @abstractmethod
    def parse(self, page_html: str, url: str) -> List[Listing]:
        ...

    def fetch_listings(self, criteria: Criteria) -> List[Listing]:
        try:
            from playwright.sync_api import sync_playwright  # lazy
        except Exception:
            logger.warning(
                "[%s] Playwright nicht verfügbar — JS-Quelle übersprungen "
                "(HTML-Quellen laufen normal weiter).", self.name,
            )
            return []

        all_listings: List[Listing] = []
        seen = set()

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=_CHROMIUM_ARGS)
                try:
                    context = browser.new_context(
                        user_agent=(
                            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="de-DE",
                        viewport={"width": 1366, "height": 900},
                    )
                    for url in self.start_urls():
                        html, render_ok = self._render(context, url)
                        if not render_ok:
                            logger.warning("[%s] render_ok=False url=%s", self.name, url)
                            continue
                        try:
                            listings = self.parse(html, url)
                        except Exception as e:
                            logger.exception("[%s] Parse-Fehler %s: %s", self.name, url, e)
                            continue
                        links = sum(1 for l in listings if l.url)
                        logger.info(
                            "[%s] render_ok=True listings_found=%d links_extracted=%d url=%s",
                            self.name, len(listings), links, url,
                        )
                        for l in listings:
                            if l.id not in seen:
                                seen.add(l.id)
                                all_listings.append(l)
                finally:
                    browser.close()
        except Exception as e:
            logger.exception("[%s] Browser-Fehler: %s", self.name, e)
            return all_listings

        return all_listings

    def _render(self, context, url: str) -> Tuple[str, bool]:
        """Lädt eine URL gerendert. Gibt (html, ok) zurück."""
        for attempt in range(1, self.retries + 1):
            page = context.new_page()
            try:
                page.set_default_timeout(self.wait_timeout_ms)
                page.goto(url, timeout=self.nav_timeout_ms, wait_until="domcontentloaded")
                if self.wait_selector:
                    try:
                        page.wait_for_selector(self.wait_selector, timeout=self.wait_timeout_ms)
                    except Exception:
                        # Kein Listing-Selektor erschienen → Diagnose, aber HTML trotzdem liefern
                        logger.info(
                            "[%s] wait_selector '%s' nicht erschienen (Versuch %d/%d)",
                            self.name, self.wait_selector, attempt, self.retries,
                        )
                html = page.content()
                page.close()
                return html, True
            except Exception as e:
                logger.warning("[%s] Render-Fehler (Versuch %d/%d): %s",
                               self.name, attempt, self.retries, e)
                try:
                    page.close()
                except Exception:
                    pass
        return "", False

    def diagnostic_dump(self, soup) -> None:
        """Hilft beim Finden von Selektoren, wenn parse() nichts findet."""
        from collections import Counter
        c = Counter()
        for el in soup.find_all(["article", "li", "div", "a"]):
            cl = " ".join(el.get("class", []))
            t = el.get_text(" ", strip=True)
            if ("€" in t or "m²" in t or "Zimmer" in t) and len(t) < 400:
                c[f"{el.name}.{cl[:50]}"] += 1
        for k, n in c.most_common(8):
            logger.info("[%s] DIAG [%dx] %s", self.name, n, k)
