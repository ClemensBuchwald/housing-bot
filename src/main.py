"""Housing Bot — KI-gestützter Dauersuchassistent.

Zwei Threads:
  1. Telegram-Thread:  empfängt Aufträge und Befehle (getUpdates-Polling)
  2. Scraping-Thread:  läuft nur wenn aktiver Auftrag existiert

Starten:
  python -m src.main
  python -m src.main --once --mock   (Einmaliger Test mit Mock-Daten)
"""
from __future__ import annotations

import argparse
import functools
import json
import logging
import os
import re
import threading
import time
from typing import List

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("housing_bot")

from src.config import load_criteria
from src.evaluator import evaluate_listing
from src.models import Listing
from src.notifications import NotificationService
from src.agent import build_sources_text
from src.scrapers.base import BaseScraper
from src.scrapers.charlotte import CharlotteScraper
from src.scrapers.degewo import DegewoScraper
from src.scrapers.gesobau import GESOBAUScraper
from src.scrapers.gewobag import GewobagScraper
from src.scrapers.immowelt import ImmoweltScraper
from src.scrapers.inberlinwohnen import InBerlinWohnenScraper
from src.scrapers.is24 import IS24Scraper
from src.scrapers.is24_detail import enrich_listing, get_contact
from src.scrapers.kleinanzeigen import KleinanzeigenScraper
from src.scrapers.mock import MockScraper
from src.scrapers.vonovia import VonoviaScraper
from src.scrapers.wbm import WBMScraper
from src.store import Store
from src.telegram_handler import TelegramHandler


def build_scrapers(use_mock: bool) -> List[BaseScraper]:
    """Reihenfolge = Meldegeschwindigkeit.

    Der Zyklus läuft sequenziell, also bestimmt die Position, wie alt ein Inserat
    beim Alert schon ist. Gemessen: inberlinwohnen ~61 s, degewo ~51 s — liefen sie
    zuerst, wurde IS24 (die ergiebigste Quelle, ~11 s) erst nach ~2,5 min abgefragt.
    Daher: schnelle, ertragreiche Quellen zuerst, träge Quellen ans Ende.
    """
    if use_mock:
        return [MockScraper()]
    return [
        # --- Schnell + hohe Ausbeute: zuerst, damit Alerts früh rausgehen ---
        IS24Scraper(),            # ~11 s, größte Quelle (Mobile-API, CW-Geocodes)
        ImmoweltScraper(),        # ~15 s, großes Portal (CW-Ortsteilsuche)
        VonoviaScraper(),         # ~11 s, Vonovia/Deutsche Wohnen (JSON-API)
        # --- Schnell, aber selten Treffer ---
        CharlotteScraper(),       # ~2 s, Genossenschaft mit CW-Bestand (kein Portal-Duplikat)
        WBMScraper(),             # ~8 s
        GESOBAUScraper(),         # ~6 s
        GewobagScraper(),         # ~6 s
        # --- Träge: ans Ende, verzögern so niemanden ---
        DegewoScraper(),          # ~51 s (TYPO3-HTML)
        KleinanzeigenScraper(),   # ~40 s (3 Seiten je Ort)
        InBerlinWohnenScraper(),  # ~61 s (alle 6 Landeseigenen, viele Seiten)
        # Heimstaden + GCP deaktiviert: Heimstaden ist ein IS24-iframe-Widget (Duplikat),
        # GCP über Playwright nicht zuverlässig filterbar + 0 CW-Ausbeute.
        # Playwright-Basis bleibt für künftige echte JS-Quellen (siehe docs/sources.md).
    ]


# Angebotsarten, die bei einer Dauermiet-Suche nie passen. Sie machen einen
# erheblichen Teil der Rohtreffer aus (gemessen: 14% Tauschwohnungen) und wurden
# bisher teuer von der KI aussortiert.
_IMMER_AUSGESCHLOSSEN = [
    "tauschwohnung", "wohnungstausch", "tausche wohnung", "tausch gegen",
    "zwischenmiete", "auf zeit",
]

# "möbliert" darf NICHT blind blocken — es steckt auch in "unmöbliert",
# und genau das ist ja gewünscht. Daher negative Vorprüfung.
_MOEBLIERT_RE = re.compile(r"(?<!un)(?<!nicht )m[öo]bliert", re.IGNORECASE)


class _StundenBudget:
    """Gleitendes Stundenlimit — unabhängig davon, wie oft ein Zyklus läuft."""

    def __init__(self, limit: int, name: str) -> None:
        self.limit = limit
        self.name = name
        self._zeiten: List[float] = []

    def _aufraeumen(self) -> None:
        grenze = time.time() - 3600
        self._zeiten = [t for t in self._zeiten if t > grenze]

    def frei(self) -> bool:
        self._aufraeumen()
        return len(self._zeiten) < self.limit

    def verbrauchen(self) -> None:
        self._zeiten.append(time.time())

    def verbraucht(self) -> int:
        self._aufraeumen()
        return len(self._zeiten)


def _hat_ausschlusswort(listing: Listing, crit: dict) -> Optional[str]:
    """Prüft Titel + Merkmale gegen Ausschlusswörter. Gibt das Treffer-Wort zurück."""
    text = (listing.titel or "").lower() + " " + " ".join(str(m) for m in (listing.merkmale or [])).lower()
    for w in _IMMER_AUSGESCHLOSSEN + [str(w).lower() for w in (crit.get("ausschlusskriterien") or [])]:
        if w and w in text:
            return w
    if _MOEBLIERT_RE.search(text) and "unmöbliert" not in text and "unmobliert" not in text:
        return "möbliert"
    return None


def _passes_basic(listing: Listing, crit: dict) -> bool:
    """Regelbasierter Vorfilter — läuft VOR der KI und kostet nichts.

    Prüft harte Zahlen (Preis/Zimmer/Fläche) und Ausschlusswörter. Fehlende
    Werte führen NICHT zur Ablehnung — im Zweifel entscheidet die KI.
    """
    if _hat_ausschlusswort(listing, crit):
        return False
    preis = listing.warmmiete or listing.kaltmiete
    limit = crit.get("warmmiete_max") or crit.get("kaltmiete_max")
    if preis and limit and preis > float(limit):
        return False
    if crit.get("zimmer_min") and listing.zimmer and listing.zimmer < float(crit["zimmer_min"]):
        return False
    if crit.get("zimmer_max") and listing.zimmer and listing.zimmer > float(crit["zimmer_max"]):
        return False
    if crit.get("flaeche_min") and listing.flaeche and listing.flaeche < float(crit["flaeche_min"]):
        return False
    return True


def run_one_off_search(criteria: dict, mandate: Optional[dict] = None,
                       store: "Optional[Store]" = None, include_seen: bool = False) -> List[dict]:
    """Einmalige Live-Abfrage MIT KI-Bewertung gegen den Auftrag.

    Ablauf: scrapen → Grobfilter (Preis/Zimmer/Fläche) → bereits Gesehenes überspringen
    (außer include_seen=True) → KI bewertet jeden Kandidaten → nur passende, nach Score
    sortiert → als gesehen merken.

    include_seen=True: zeigt ALLE passenden inkl. bereits gezeigter ("zeig mir nochmal alle").
    """
    scrapers: List[BaseScraper] = [
        IS24Scraper(),
        ImmoweltScraper(),
        VonoviaScraper(),
        CharlotteScraper(),
        WBMScraper(),
        GESOBAUScraper(),
        GewobagScraper(),
        KleinanzeigenScraper(),
        InBerlinWohnenScraper(page_limit=4),
    ]
    yaml_criteria = load_criteria()

    # 1) Kandidaten einsammeln + Grobfilter
    candidates: List[Listing] = []
    for scraper in scrapers:
        try:
            listings = scraper.fetch_listings(yaml_criteria)
        except Exception:
            logger.exception("On-Demand: Fehler bei %s", scraper.name)
            continue
        for l in listings:
            if not _passes_basic(l, criteria):
                continue
            # Bereits Gesehenes überspringen — außer der Nutzer will explizit ALLE
            if store and not include_seen and store.is_known(l.id, l.portal):
                continue
            candidates.append(l)

    candidates = candidates[:12]  # KI-Budget begrenzen

    # Kandidaten sofort reservieren — sonst bewertet die parallel laufende
    # Dauersuche dieselben Inserate nochmal und meldet sie doppelt.
    if store and not include_seen:
        candidates = [l for l in candidates if store.claim(l)]

    # 1b) Detaildaten nachladen (Etage/Balkon/Keller/Aufzug/Beschreibung),
    #     damit die KI Kriterien wie "kein Erdgeschoss" wirklich prüfen kann
    enrich_candidates(candidates)

    logger.info("On-Demand: %d neue Kandidaten → KI-Bewertung", len(candidates))

    # 2) KI-Bewertung gegen den Auftrag
    eval_mandate = mandate or {"raw_text": _criteria_to_text(criteria), "structured": criteria}
    treffer: List[dict] = []
    for l in candidates:
        try:
            ev = evaluate_listing(l, eval_mandate)
        except Exception:
            logger.exception("On-Demand: Bewertung fehlgeschlagen für %s", l.id)
            continue
        if not ev.passt or ev.score < 40:
            continue
        # Als gesehen/gemeldet merken → kein Doppel in Dauersuche oder nächster Sofort-Abfrage
        if store:
            try:
                store.save_listing(l)
                store.save_evaluation(l.id, l.portal, (mandate or {}).get("id", 0), ev.__dict__)
                store.mark_notified(l.id, l.portal)
            except Exception:
                logger.debug("On-Demand: Persistenz fehlgeschlagen für %s", l.id, exc_info=True)
        treffer.append({
            "titel": l.titel,
            "ort": l.stadtteil or l.stadt,
            "warmmiete": l.warmmiete,
            "kaltmiete": l.kaltmiete,
            "flaeche": l.flaeche,
            "zimmer": l.zimmer,
            "quelle": l.portal,
            "url": l.url,
            "score": ev.score,
            "vorteile": ev.vorteile[:2],
            "nachteile": (ev.nachteile or ev.offene_punkte)[:2],
            "empfehlung": ev.empfehlung,
        })

    treffer.sort(key=lambda t: -t["score"])
    logger.info("On-Demand: %d passende Treffer nach KI-Bewertung", len(treffer))
    return treffer[:8]


def enrich_candidates(listings: List[Listing]) -> int:
    """Lädt Detaildaten nach (aktuell IS24): Etage, Balkon, Keller, Aufzug, Beschreibung.

    Ohne diese Daten kann die KI Kriterien wie "kein Erdgeschoss" oder "Balkon"
    nicht belastbar prüfen. Fehler sind unkritisch — Listing bleibt dann roh.
    """
    n = 0
    for l in listings:
        try:
            if enrich_listing(l):
                n += 1
        except Exception:
            logger.debug("Enrichment fehlgeschlagen für %s", l.id, exc_info=True)
    if n:
        logger.info("Detaildaten für %d/%d Inserate nachgeladen", n, len(listings))
    return n


def _criteria_to_text(c: dict) -> str:
    parts = []
    if c.get("zielorte"):
        parts.append("in " + " oder ".join(c["zielorte"]))
    if c.get("zimmer_min"):
        parts.append(f"ab {c['zimmer_min']} Zimmer")
    if c.get("flaeche_min"):
        parts.append(f"ab {c['flaeche_min']} m²")
    if c.get("warmmiete_max"):
        parts.append(f"max {c['warmmiete_max']} € warm")
    if c.get("ausschlusskriterien"):
        parts.append("ohne: " + ", ".join(c["ausschlusskriterien"]))
    if c.get("wunschkriterien"):
        parts.append("Wunsch: " + ", ".join(c["wunschkriterien"]))
    return "Wohnungssuche " + ", ".join(parts) if parts else "Wohnungssuche"


# Schutz vor Nachrichtenflut: Wenn eine Quelle neu dazukommt, sind schlagartig
# hunderte Inserate "neu". Pro Zyklus werden daher nur begrenzt viele bewertet
# und gemeldet — der Rest bleibt ungesehen und kommt im nächsten Zyklus dran.
MAX_EVAL_PRO_ZYKLUS = int(os.getenv("MAX_EVAL_PRO_ZYKLUS", "40"))
MAX_ALERTS_PRO_ZYKLUS = int(os.getenv("MAX_ALERTS_PRO_ZYKLUS", "8"))
# Zeitbasierte Obergrenzen. Wichtig seit der Schnellspur: Zyklen laufen jetzt
# 5x häufiger, ein reines Pro-Zyklus-Limit würde also 5x so viel durchlassen.
MAX_EVAL_PRO_STUNDE = int(os.getenv("MAX_EVAL_PRO_STUNDE", "60"))
MAX_ALERTS_PRO_STUNDE = int(os.getenv("MAX_ALERTS_PRO_STUNDE", "12"))

EVAL_BUDGET = _StundenBudget(MAX_EVAL_PRO_STUNDE, "Bewertungen")
ALERT_BUDGET = _StundenBudget(MAX_ALERTS_PRO_STUNDE, "Alerts")
# Je Quelle ein eigenes Stundenbudget — eine Quelle darf das Gesamtbudget nicht
# allein aufbrauchen, sonst kommen die übrigen Quellen bei Rückstau nie dran.
QUELLEN_BUDGET: dict = {}


def _quellen_budget(name: str) -> _StundenBudget:
    if name not in QUELLEN_BUDGET:
        QUELLEN_BUDGET[name] = _StundenBudget(MAX_EVAL_PRO_QUELLE_STUNDE, f"Bewertungen/{name}")
    return QUELLEN_BUDGET[name]
# Damit eine grosse Quelle (IS24: 150 Inserate) bei Rückstau nicht das ganze
# Budget frisst und die übrigen Quellen aushungert. Seit der Schnellspur laufen
# die schnellen Quellen 5x häufiger — deshalb zusätzlich stundenbasiert.
MAX_EVAL_PRO_QUELLE = int(os.getenv("MAX_EVAL_PRO_QUELLE", "15"))
MAX_EVAL_PRO_QUELLE_STUNDE = int(os.getenv("MAX_EVAL_PRO_QUELLE_STUNDE", "25"))


SAMMELMELDUNG_BUDGET = _StundenBudget(2, "Sammelmeldungen")


def _sammelmeldung(notifier: NotificationService, anzahl: int) -> None:
    """Hinweis auf zurückgestellte Treffer — höchstens 2x pro Stunde.

    Ohne Drosselung würde bei vollem Alert-Budget jeder 2-Minuten-Zyklus eine
    eigene "…und 1 weitere"-Nachricht schicken und den Chat genau durch die
    Anti-Flut-Logik fluten.
    """
    if not SAMMELMELDUNG_BUDGET.frei():
        return
    SAMMELMELDUNG_BUDGET.verbrauchen()
    notifier.send_text(
        f"… {anzahl} weitere passende Angebote sind zurückgestellt.\n"
        f"Ich melde sie nach, sobald wieder Luft ist — oder sag „zeig mir alle“."
    )


def _nachholen(store: Store, notifier: NotificationService) -> int:
    """Sendet zurückgestellte Treffer nach — ohne neue KI-Bewertung.

    Die vollständige Bewertung liegt in match_log.evaluation, es entstehen also
    keine Token-Kosten. Läuft zu Beginn jedes Zyklus, solange Budget frei ist.
    """
    from src.evaluator import Evaluation
    nachgeholt = 0
    for row in store.get_pending_matches(limit=MAX_ALERTS_PRO_ZYKLUS):
        if not ALERT_BUDGET.frei():
            break
        try:
            ev_dict = json.loads(row["evaluation"]) if row["evaluation"] else {}
            evaluation = Evaluation.from_dict(ev_dict)
            listing = Listing(
                id=row["id"], portal=row["portal"], url=row["url"], titel=row["titel"],
                stadt=row["stadt"], stadtteil=row["stadtteil"],
                kaltmiete=row["kaltmiete"], warmmiete=row["warmmiete"],
                flaeche=row["flaeche"], zimmer=row["zimmer"],
                merkmale=json.loads(row["merkmale"]) if row["merkmale"] else [],
            )
        except Exception:
            logger.debug("Nachholen: Datensatz unlesbar (%s)", row["id"], exc_info=True)
            continue
        if notifier.send_evaluation(listing, evaluation):
            ALERT_BUDGET.verbrauchen()
            store.mark_notified(listing.id, listing.portal)
            nachgeholt += 1
    if nachgeholt:
        logger.info("%d zurückgestellte Treffer nachgeholt", nachgeholt)
    return nachgeholt


def run_scraping_cycle(
    scrapers: List[BaseScraper],
    store: Store,
    notifier: NotificationService,
    mandate: dict,
) -> int:
    """Führt einen kompletten Scraping-Zyklus gegen den aktiven Auftrag durch."""
    hits = 0
    unterdrueckt = 0
    # Zuerst offene Treffer aus früheren Zyklen zustellen — sie sind älter und
    # kosten nichts, weil die Bewertung bereits vorliegt.
    _nachholen(store, notifier)
    evaluated = 0
    criteria = load_criteria()  # Hot-reload aus criteria.yaml

    for scraper in scrapers:
        if evaluated >= MAX_EVAL_PRO_ZYKLUS:
            break
        logger.info("Scraper %s …", scraper.name)
        try:
            listings: List[Listing] = scraper.fetch_listings(criteria)
        except Exception:
            logger.exception("Fehler bei Scraper %s", scraper.name)
            continue

        logger.info("%d Inserate von %s", len(listings), scraper.name)
        eval_quelle = 0

        for listing in listings:
            if evaluated >= MAX_EVAL_PRO_ZYKLUS:
                logger.info("Bewertungslimit (%d) erreicht — Rest folgt im nächsten Zyklus",
                            MAX_EVAL_PRO_ZYKLUS)
                break
            if not EVAL_BUDGET.frei():
                logger.info("Stundenbudget Bewertungen (%d) ausgeschöpft — Rest folgt später",
                            MAX_EVAL_PRO_STUNDE)
                break
            if eval_quelle >= MAX_EVAL_PRO_QUELLE or not _quellen_budget(scraper.name).frei():
                logger.info("[%s] Quellen-Limit erreicht (Zyklus %d / Stunde %d) — Rest folgt später",
                            scraper.name, MAX_EVAL_PRO_QUELLE, MAX_EVAL_PRO_QUELLE_STUNDE)
                break

            # Atomar reservieren statt "prüfen, später speichern" — sonst sieht
            # die parallel laufende Sofort-Suche dasselbe Inserat auch als neu.
            if not store.claim(listing):
                continue

            krit = mandate.get("structured") or {}

            # Frühfilter auf den bereits vorhandenen Feldern (Zimmer/Fläche/Kaltmiete):
            # spart den Detail-Request UND die KI-Bewertung für offensichtliche Ausreißer.
            if not _passes_basic(listing, krit):
                logger.debug("Frühfilter [%s]: harte Kriterien verfehlt", listing.id)
                continue

            # Detaildaten nachladen (Etage/Balkon/Keller/Aufzug/Warmmiete) — erst NACH
            # Dedup und Frühfilter, damit nur relevante Inserate einen Request kosten
            try:
                enrich_listing(listing)
                store.update_listing_felder(listing)
            except Exception:
                logger.debug("Enrichment fehlgeschlagen für %s", listing.id, exc_info=True)

            # Zweiter Durchgang: jetzt ist auch die Warmmiete bekannt (kam erst
            # aus dem Detail-Abruf) — ohne KI-Kosten erneut hart prüfen.
            if not _passes_basic(listing, krit):
                logger.debug("Vorfilter [%s]: Warmmiete/Kriterien verfehlt", listing.id)
                continue

            # KI-Bewertung gegen aktiven Auftrag
            evaluation = evaluate_listing(listing, mandate)
            evaluated += 1
            eval_quelle += 1
            EVAL_BUDGET.verbrauchen()
            _quellen_budget(scraper.name).verbrauchen()
            store.save_evaluation(listing.id, listing.portal, mandate.get("id", 0), evaluation.__dict__)

            if not evaluation.passt or evaluation.score < 30:
                logger.debug(
                    "Kein Match [%s] score=%d: %s",
                    listing.id, evaluation.score, evaluation.kurzfazit,
                )
                continue

            logger.info(
                "MATCH! [%s] '%s' — Score %d — %s",
                listing.id, listing.titel, evaluation.score, evaluation.empfehlung,
            )
            hits += 1

            # Alert-Bremse: pro Zyklus UND gleitend pro Stunde. Unterdrückte
            # Treffer bleiben mit notified_at = NULL liegen und werden zu Beginn
            # eines späteren Zyklus nachgeholt (_nachholen) — sie gehen NICHT
            # verloren und kosten dabei auch keine neue KI-Bewertung.
            if hits > MAX_ALERTS_PRO_ZYKLUS or not ALERT_BUDGET.frei():
                unterdrueckt += 1
                continue

            sent = notifier.send_evaluation(listing, evaluation)
            if sent:
                ALERT_BUDGET.verbrauchen()
                store.mark_notified(listing.id, listing.portal)
            else:
                # Versand fehlgeschlagen: ebenfalls offen lassen statt still verlieren
                unterdrueckt += 1

    if unterdrueckt:
        logger.info("%d Treffer offen (Zyklus %d / Stunde %d) — werden nachgeholt",
                    unterdrueckt, MAX_ALERTS_PRO_ZYKLUS, MAX_ALERTS_PRO_STUNDE)
        _sammelmeldung(notifier, unterdrueckt)

    return hits


# Schnellspur: Quellen, die in Sekunden antworten und die meisten Treffer liefern.
# Sie werden häufig gepollt; die trägen Quellen laufen weiter im langsamen Takt.
# Gemessen: ~2 neue CW-Inserate/Stunde zu Geschäftszeiten — bei 10-Minuten-Takt
# ist ein Treffer im Mittel 5 Minuten alt, bei 2 Minuten nur noch eine.
SCHNELLSPUR = {"is24", "immowelt", "vonovia"}


def scraping_thread(scrapers: List[BaseScraper], store: Store, notifier: NotificationService,
                    stop_event: threading.Event, poll_interval: int,
                    fast_interval: Optional[int] = None) -> None:
    """Ein Thread, zwei Takte.

    Bewusst einthreadig: eine zweite Nebenläufigkeit auf Store und Telegram würde
    Dedup und Alert-Limits unterlaufen. Stattdessen läuft die Schleife im schnellen
    Takt und nimmt die trägen Quellen nur jeden n-ten Durchlauf dazu.
    """
    fast_interval = fast_interval or poll_interval
    schnell = [s for s in scrapers if s.name in SCHNELLSPUR]
    traege = [s for s in scrapers if s.name not in SCHNELLSPUR]
    # Wie viele Schnell-Durchläufe pro vollem Durchlauf
    voll_alle_n = max(1, round(poll_interval / fast_interval)) if fast_interval else 1

    logger.info(
        "Scraping-Thread gestartet — Schnellspur %s alle %ds, alle Quellen alle %ds",
        [s.name for s in schnell] or "—", fast_interval, poll_interval,
    )

    runde = 0
    while not stop_event.is_set():
        mandate = store.get_any_active_mandate()
        if mandate:
            voller_lauf = (runde % voll_alle_n == 0)
            aktive = scrapers if voller_lauf else schnell
            if aktive:
                logger.info("Aktiver Auftrag — %s Zyklus (%d Quellen)",
                            "voller" if voller_lauf else "Schnell", len(aktive))
                try:
                    hits = run_scraping_cycle(aktive, store, notifier, mandate)
                    logger.info("%d neue Treffer in diesem Zyklus", hits)
                except Exception:
                    logger.exception("Fehler im Scraping-Zyklus")
        else:
            # Nur beim vollen Takt loggen, sonst flutet es die Logs
            if runde % voll_alle_n == 0:
                logger.info("Kein aktiver Auftrag — Bot wartet")
        runde += 1

        # Warte in kleinen Schritten, damit stop_event schnell reagiert
        for _ in range(fast_interval):
            if stop_event.is_set():
                break
            time.sleep(1)

    logger.info("Scraping-Thread beendet")


def telegram_thread(handler: TelegramHandler, stop_event: threading.Event) -> None:
    """Hintergrund-Thread: Telegram getUpdates-Polling."""
    logger.info("Telegram-Thread gestartet")
    while not stop_event.is_set():
        try:
            handler.poll_once()
        except Exception:
            logger.exception("Fehler im Telegram-Thread")
        time.sleep(2)
    logger.info("Telegram-Thread beendet")


def main() -> None:
    parser = argparse.ArgumentParser(description="Housing Bot")
    parser.add_argument("--once", action="store_true", help="Einmalig ausführen")
    parser.add_argument("--mock", action="store_true", help="Mock-Scraper")
    args = parser.parse_args()

    poll_interval = int(os.getenv("POLL_INTERVAL", "10")) * 60
    # Schnellspur-Takt in Sekunden (0/leer = aus, dann läuft alles im alten Takt)
    fast_interval = int(os.getenv("FAST_POLL_SECONDS", "120")) or poll_interval
    fast_interval = min(fast_interval, poll_interval)
    scrapers = build_scrapers(use_mock=args.mock)
    store = Store()
    notifier = NotificationService()
    # On-Demand-Suche nur im echten Betrieb (nicht im Mock-Modus)
    # Sofort-Suche mit Store verbinden (Dedup + Persistenz der gezeigten Treffer)
    search_fn = None if args.mock else functools.partial(run_one_off_search, store=store)
    sources_text = build_sources_text([s.name for s in scrapers])
    tg_handler = TelegramHandler(store, search_fn=search_fn, sources_text=sources_text,
                                 contact_fn=get_contact)

    logger.info(
        "Housing Bot gestartet. Scraper: %s. Mock: %s",
        [s.name for s in scrapers], args.mock,
    )

    if args.once:
        # Einmaliger Lauf: vorhandenen Auftrag nutzen oder Mock-Mandate
        mandate = store.get_any_active_mandate()
        if not mandate:
            logger.warning("Kein aktiver Auftrag — Einmallauf mit leerem Auftrag")
            mandate = {"raw_text": "Test", "structured": {}, "id": 0}
        hits = run_scraping_cycle(scrapers, store, notifier, mandate)
        logger.info("Einmaliger Lauf abgeschlossen. %d Treffer.", hits)
        store.close()
        return

    # Normalbetrieb: zwei Threads
    stop_event = threading.Event()

    t_scraping = threading.Thread(
        target=scraping_thread,
        args=(scrapers, store, notifier, stop_event, poll_interval, fast_interval),
        daemon=True,
        name="scraping",
    )
    t_telegram = threading.Thread(
        target=telegram_thread,
        args=(tg_handler, stop_event),
        daemon=True,
        name="telegram",
    )

    t_scraping.start()
    t_telegram.start()

    logger.info("Bot läuft. Warte auf Auftrag per Telegram. Stoppen mit Ctrl+C.")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Bot wird gestoppt …")
        stop_event.set()
    finally:
        t_scraping.join(timeout=10)
        t_telegram.join(timeout=5)
        store.close()
        logger.info("Bot beendet.")


if __name__ == "__main__":
    main()
