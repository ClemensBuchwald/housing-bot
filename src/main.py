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
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


class _GeheimnisFilter(logging.Filter):
    """Entfernt Zugangsdaten aus Logzeilen.

    httpx protokolliert jede Anfrage-URL auf INFO-Ebene. Bei der Telegram-API
    steht der Bot-Token im PFAD der URL — er landete damit im Klartext in den
    Container-Logs, die jeder mit Docker-Zugriff lesen kann und die in jedem
    Logsammler weiterleben.

    Der Filter greift an der Wurzel, damit er unabhängig davon wirkt, welche
    Bibliothek die Zeile erzeugt hat.
    """

    _MUSTER = (
        re.compile(r"bot\d{6,}:[A-Za-z0-9_-]{20,}"),      # Telegram
        re.compile(r"sk-ant-[A-Za-z0-9_-]{10,}"),         # Anthropic
    )

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            nachricht = record.getMessage()
        except Exception:
            return True
        bereinigt = nachricht
        for muster in self._MUSTER:
            bereinigt = muster.sub(lambda m: m.group(0)[:8] + "…<entfernt>", bereinigt)
        if bereinigt != nachricht:
            record.msg = bereinigt
            record.args = ()
        return True


for _handler in logging.getLogger().handlers:
    _handler.addFilter(_GeheimnisFilter())

logger = logging.getLogger("housing_bot")

from src.config import load_criteria
from src.evaluator import evaluate_listing
from src.health import Health
from src.llm import get_breaker
from src.llm.errors import LLMError, zu_llm_fehler
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
from src.store import EVAL_EXPIRED, Store
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
        except Exception as e:
            # Auch hier ist das Inserat durch claim() schon als gesehen vermerkt.
            # Ohne Warteschlangeneintrag wäre es dauerhaft verloren.
            if store:
                _bewertung_zurueckstellen(store, l, (mandate or {}).get("id"), e)
            else:
                logger.warning("On-Demand: Bewertung fehlgeschlagen für %s: %s", l.id, e)
            continue
        # Das Modell hat entschieden — ob dafür oder dagegen ist gleichgültig:
        # eine offene Wiedervorlage aus einem früheren Fehlversuch ist damit
        # erledigt. Stünde das erst weiter unten, bliebe ein abgelehntes Inserat
        # für immer in der Warteschlange stehen.
        if store:
            store.resolve_eval(l.id, l.portal)

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


# Wartezeit bis zum nächsten Bewertungsversuch, je Fehlerart. Bewusst grob:
# Die Warteschlange wird in dieser Phase nur befüllt, nicht automatisch abgearbeitet.
_RETRY_ABSTAND_S = {"ratelimit": 60, "temporaer": 120, "protokoll": 300}
_RETRY_DECKEL_S = 900


def _naechster_versuch(fehler: LLMError, versuche: int = 1) -> Optional[str]:
    """Frühester Zeitpunkt für einen erneuten Versuch — None, wenn Warten nicht hilft.

    Beim Rate-Limit gilt die Angabe der Gegenseite. Sonst wird gestaffelt: Hält
    die Störung an, wächst der Abstand, statt im gleichen Takt weiter anzuklopfen.
    """
    if not fehler.retryable:
        return None
    vorgabe = getattr(fehler, "retry_after", None)
    if vorgabe:
        wartezeit = float(vorgabe)
    else:
        basis = _RETRY_ABSTAND_S.get(fehler.kategorie, 120)
        wartezeit = basis * (2 ** max(0, versuche - 1))
    # Gedeckelt: Ein großzügiges Retry-After der Gegenseite darf einen Eintrag
    # nicht auf Stunden hinausschieben.
    wartezeit = min(float(wartezeit), float(_RETRY_DECKEL_S))
    return (datetime.now() + timedelta(seconds=wartezeit)).isoformat()


def _bewertung_zurueckstellen(store: Store, listing: Listing, mandate_id: Optional[int],
                              e: Exception) -> LLMError:
    """Technischer Fehler bei der Bewertung → Inserat bleibt offen.

    Der entscheidende Punkt: Es wird KEIN match_log-Eintrag geschrieben. Ein
    Fehler ist keine Entscheidung, also gibt es auch kein "bestanden = 0". Das
    Inserat wurde durch claim() bereits als gesehen vermerkt und käme ohne diesen
    Eintrag nie wieder — die Warteschlange ist sein einziger Rückweg.
    """
    fehler = zu_llm_fehler(e)
    vorher = store.get_eval_queue_entry(listing.id, listing.portal)
    versuche = (int(vorher["retry_count"]) if vorher else 0) + (1 if fehler.verbraucht_versuch else 0)
    # Die Warteschlange verknüpft mit listings — ohne diesen Datensatz wäre der
    # Eintrag nie abarbeitbar. In der Dauersuche hat claim() ihn schon angelegt;
    # in der Sofort-Suche mit "zeig mir alle" wird nicht reserviert, dort fehlt er.
    # INSERT OR IGNORE, überschreibt also nie etwas Bestehendes.
    store.save_listing(listing)
    store.enqueue_eval(
        listing.id, listing.portal, mandate_id,
        fehler=f"{type(fehler).__name__}: {fehler}",
        verbraucht_versuch=fehler.verbraucht_versuch,
        max_versuche=fehler.max_versuche,
        retryable=fehler.retryable,
        next_retry_at=_naechster_versuch(fehler, versuche),
    )
    logger.warning("Bewertung zurückgestellt [%s/%s] (%s): %s",
                   listing.portal, listing.id, fehler.kategorie, fehler)
    return fehler


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
        # Zweite Sperre unmittelbar vor dem Versand: Zwischen der Abfrage oben und
        # diesem Moment kann die Sofort-Suche im Telegram-Thread dasselbe Inserat
        # bereits verschickt und als gemeldet vermerkt haben.
        if store.ist_gemeldet(listing.id, listing.portal):
            continue

        if notifier.send_evaluation(listing, evaluation):
            ALERT_BUDGET.verbrauchen()
            store.mark_notified(listing.id, listing.portal)
            nachgeholt += 1
    if nachgeholt:
        logger.info("%d zurückgestellte Treffer nachgeholt", nachgeholt)
    return nachgeholt


# --- Wiedervorlage gescheiterter Bewertungen ------------------------------
#
# Grundsatz: Neue Inserate zuerst. Ein Retry betrifft ein Inserat, das wir schon
# einmal gesehen haben; ein neues könnte in Minuten weg sein. Die Wiedervorlage
# läuft deshalb am ENDE des Zyklus und nur mit dem, was dann noch an Budget übrig ist.
MAX_RETRY_PRO_ZYKLUS = int(os.getenv("MAX_RETRY_PRO_ZYKLUS", "5"))
MAX_RETRY_PRO_STUNDE = int(os.getenv("MAX_RETRY_PRO_STUNDE", "15"))
RETRY_BUDGET = _StundenBudget(MAX_RETRY_PRO_STUNDE, "Wiedervorlagen")

# Wie lange nach dem Fund eine Meldung überhaupt noch sinnvoll ist. Eine
# Wohnung, die vor zwei Stunden erschien, ist in Berlin praktisch vergeben —
# eine späte Meldung wäre nur noch Lärm.
RETRY_TTL_MINUTEN = int(os.getenv("RETRY_TTL_MINUTEN", "60"))


def _ist_abgelaufen(seen_at: Optional[str], ttl_minuten: int = None) -> bool:
    """Liegt der ursprüngliche Fund zu lange zurück?"""
    ttl = RETRY_TTL_MINUTEN if ttl_minuten is None else ttl_minuten
    if not seen_at:
        return False
    try:
        gefunden = datetime.fromisoformat(str(seen_at))
    except (TypeError, ValueError):
        return False
    return (datetime.now() - gefunden) > timedelta(minutes=ttl)


def _listing_aus_zeile(row) -> Listing:
    """Baut das Inserat aus der Warteschlangenzeile — ohne erneutes Scrapen."""
    return Listing(
        id=row["listing_id"], portal=row["portal"], url=row["url"] or "",
        titel=row["titel"] or "", stadt=row["stadt"] or "Berlin",
        stadtteil=row["stadtteil"], kaltmiete=row["kaltmiete"],
        warmmiete=row["warmmiete"], flaeche=row["flaeche"], zimmer=row["zimmer"],
        merkmale=json.loads(row["merkmale"]) if row["merkmale"] else [],
    )


def _retries_abarbeiten(store: Store, mandate: dict) -> int:
    """Arbeitet offene Bewertungen nach — neueste zuerst.

    Bewertet ausschließlich; verschickt nichts. Ein Treffer wird ganz normal in
    match_log geschrieben und geht danach über denselben Weg wie jeder andere
    zurückgestellte Treffer hinaus (_nachholen). So entsteht keine zweite
    Versandlogik und damit auch keine zweite Gelegenheit für eine Doppelmeldung.
    """
    if not RETRY_BUDGET.frei() or not EVAL_BUDGET.frei():
        return 0

    verarbeitet = 0
    abgelaufen = 0
    # Etwas mehr holen als verarbeitet wird: Abgelaufene fallen unterwegs weg.
    for row in store.get_due_evaluations(limit=MAX_RETRY_PRO_ZYKLUS * 4):
        if verarbeitet >= MAX_RETRY_PRO_ZYKLUS:
            break
        if not RETRY_BUDGET.frei() or not EVAL_BUDGET.frei():
            logger.info("Wiedervorlage: Budget erschöpft — Rest folgt später")
            break

        if _ist_abgelaufen(row["seen_at"]):
            store.set_eval_status(row["listing_id"], row["portal"], EVAL_EXPIRED,
                                  f"Fund älter als {RETRY_TTL_MINUTEN} min")
            abgelaufen += 1
            continue

        # Bereits gemeldet? Dann ist nichts mehr offen.
        if row["notified_at"]:
            store.resolve_eval(row["listing_id"], row["portal"])
            continue

        listing = _listing_aus_zeile(row)
        RETRY_BUDGET.verbrauchen()
        EVAL_BUDGET.verbrauchen()
        verarbeitet += 1

        # Bewertet wird gegen den AKTUELLEN Auftrag, nicht gegen den von damals:
        # Maßgeblich ist, was der Nutzer heute sucht.
        try:
            evaluation = evaluate_listing(listing, mandate)
        except Exception as e:
            _bewertung_zurueckstellen(store, listing, mandate.get("id"), e)
            continue

        store.save_evaluation(listing.id, listing.portal, mandate.get("id", 0),
                              evaluation.__dict__)
        store.resolve_eval(listing.id, listing.portal)
        logger.info("Wiedervorlage [%s/%s]: %s (Score %d)", listing.portal, listing.id,
                    "Treffer" if evaluation.passt else "abgelehnt", evaluation.score)

    if abgelaufen:
        logger.info("%d Wiedervorlagen verfallen (älter als %d min)",
                    abgelaufen, RETRY_TTL_MINUTEN)
    return verarbeitet


def run_scraping_cycle(
    scrapers: List[BaseScraper],
    store: Store,
    notifier: NotificationService,
    mandate: dict,
    health: "Optional[Health]" = None,
) -> int:
    """Führt einen kompletten Scraping-Zyklus gegen den aktiven Auftrag durch."""
    begonnen = time.time()
    hits = 0
    unterdrueckt = 0
    # Zuerst offene Treffer aus früheren Zyklen zustellen — sie sind älter und
    # kosten nichts, weil die Bewertung bereits vorliegt.
    _nachholen(store, notifier)
    evaluated = 0
    inserate_gesamt = 0
    quellen_fehler = 0
    criteria = load_criteria()  # Hot-reload aus criteria.yaml

    for scraper in scrapers:
        if health:
            health.herzschlag("scraping")
        if evaluated >= MAX_EVAL_PRO_ZYKLUS:
            break
        logger.info("Scraper %s …", scraper.name)
        try:
            listings: List[Listing] = scraper.fetch_listings(criteria)
        except Exception:
            # Ein technischer Ausfall ist NICHT dasselbe wie "nichts gefunden".
            # Ohne diese Unterscheidung sähe eine reihenweise kaputte Quelle im
            # Log genauso aus wie ein ruhiger Markt.
            quellen_fehler += 1
            logger.exception("Fehler bei Scraper %s", scraper.name)
            continue

        inserate_gesamt += len(listings)
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

            # KI-Bewertung gegen aktiven Auftrag. Gezählt wird der VERSUCH, nicht
            # erst das Ergebnis — sonst liefe eine Dauerstörung (etwa ein
            # ungültiger Schlüssel) ungebremst durch alle Inserate.
            evaluated += 1
            eval_quelle += 1
            EVAL_BUDGET.verbrauchen()
            _quellen_budget(scraper.name).verbrauchen()

            try:
                evaluation = evaluate_listing(listing, mandate)
            except Exception as e:
                # Keine Entscheidung, also auch kein Eintrag im match_log.
                _bewertung_zurueckstellen(store, listing, mandate.get("id"), e)
                continue

            store.save_evaluation(listing.id, listing.portal, mandate.get("id", 0), evaluation.__dict__)
            # Falls dieses Inserat aus einem früheren Fehlversuch noch offen war:
            # es ist jetzt fachlich entschieden.
            store.resolve_eval(listing.id, listing.portal)

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

    if quellen_fehler:
        stufe = logger.error if quellen_fehler == len(scrapers) else logger.warning
        stufe("%d von %d Quellen technisch ausgefallen — %d Inserate insgesamt "
              "(ein Ausfall ist kein leeres Ergebnis)",
              quellen_fehler, len(scrapers), inserate_gesamt)

    # Erst jetzt, mit dem Rest des Budgets: gescheiterte Bewertungen nachholen.
    nachbewertet = _retries_abarbeiten(store, mandate)
    if nachbewertet:
        # Wieder derselbe Versandweg wie für jeden anderen offenen Treffer.
        _nachholen(store, notifier)

    if health:
        health.herzschlag("scraping")
        health.queue_zustand(store.eval_queue_zaehler())
        health.llm_zustand(_llm_info())
        health.zyklus_beendet(len(scrapers), quellen_fehler, inserate_gesamt,
                              hits, time.time() - begonnen)

    return hits


def _llm_info() -> dict:
    """Zustand des Stromkreises, falls ein umhüllter Provider aktiv ist."""
    breaker = get_breaker()
    return breaker.info() if breaker else {"zustand": "geschlossen"}


# Schnellspur: Quellen, die in Sekunden antworten und die meisten Treffer liefern.
# Sie werden häufig gepollt; die trägen Quellen laufen weiter im langsamen Takt.
# Gemessen: ~2 neue CW-Inserate/Stunde zu Geschäftszeiten — bei 10-Minuten-Takt
# ist ein Treffer im Mittel 5 Minuten alt, bei 2 Minuten nur noch eine.
SCHNELLSPUR = {"is24", "immowelt", "vonovia"}


def scraping_thread(scrapers: List[BaseScraper], store: Store, notifier: NotificationService,
                    stop_event: threading.Event, poll_interval: int,
                    fast_interval: Optional[int] = None,
                    health: "Optional[Health]" = None) -> None:
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
        # Der GESAMTE Schleifenkörper ist abgesichert. Vorher lag
        # get_any_active_mandate() ausserhalb: Ein einzelner Datenbankfehler
        # beendete den Thread lautlos — der Bot lief dann scheinbar weiter,
        # suchte aber nie wieder.
        try:
            if health:
                health.herzschlag("scraping")
            mandate = store.get_any_active_mandate()
            if health:
                health.db_ok()

            if mandate:
                voller_lauf = (runde % voll_alle_n == 0)
                aktive = scrapers if voller_lauf else schnell
                if aktive:
                    logger.info("Aktiver Auftrag — %s Zyklus (%d Quellen)",
                                "voller" if voller_lauf else "Schnell", len(aktive))
                    hits = run_scraping_cycle(aktive, store, notifier, mandate, health)
                    logger.info("%d neue Treffer in diesem Zyklus", hits)
            else:
                # Nur beim vollen Takt loggen, sonst flutet es die Logs
                if runde % voll_alle_n == 0:
                    logger.info("Kein aktiver Auftrag — Bot wartet")
        except sqlite3.Error as e:
            logger.exception("Datenbankfehler im Scraping-Thread")
            if health:
                health.db_fehler(str(e))
        except Exception:
            logger.exception("Fehler im Scraping-Zyklus")
        runde += 1

        # Warte in kleinen Schritten, damit stop_event schnell reagiert
        for _ in range(fast_interval):
            if stop_event.is_set():
                break
            time.sleep(1)

    logger.info("Scraping-Thread beendet")


def telegram_thread(handler: TelegramHandler, stop_event: threading.Event,
                    health: "Optional[Health]" = None) -> None:
    """Hintergrund-Thread: Telegram getUpdates-Polling."""
    logger.info("Telegram-Thread gestartet")
    while not stop_event.is_set():
        try:
            if health:
                health.herzschlag("telegram")
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
    health = Health()
    health.herzschlag("scraping")     # Startwerte, damit der Healthcheck in der
    health.herzschlag("telegram")     # Anlaufphase nicht fälschlich Alarm schlägt

    # Einmalige Freigabe blockierter Bewertungen beim sauberen Start.
    #
    # "blocked" heisst: Ein erneuter Versuch mit UNVERÄNDERTER Konfiguration ist
    # zwecklos — typischerweise ein ungültiger Schlüssel. Genau diese
    # Konfiguration liest der Bot aber ausschliesslich beim Container-Recreate
    # neu. Ein sauberer Start ist damit der einzige Zeitpunkt, an dem sich etwas
    # geändert haben KANN, und deshalb der richtige Moment für die Freigabe.
    #
    # Freigegeben heisst nicht sofort abgearbeitet: Die Einträge unterliegen
    # danach den normalen Retry-Budgets. Besteht der Fehler fort, sind sie nach
    # dem ersten Versuch wieder blockiert — ohne Versuchsverbrauch, weil ein
    # Auth-Fehler nie zählt. Es entsteht also weder eine Sackgasse noch eine
    # Aufrufschleife.
    try:
        freigegeben = store.reaktiviere_blockierte()
        if freigegeben:
            logger.info("%d blockierte Bewertungen beim Start freigegeben", freigegeben)
    except Exception:
        logger.exception("Freigabe blockierter Bewertungen fehlgeschlagen")

    try:
        health.queue_zustand(store.eval_queue_zaehler())
    except Exception:
        logger.debug("Warteschlangenzähler nicht lesbar", exc_info=True)
    # On-Demand-Suche nur im echten Betrieb (nicht im Mock-Modus)
    # Sofort-Suche mit Store verbinden (Dedup + Persistenz der gezeigten Treffer)
    search_fn = None if args.mock else functools.partial(run_one_off_search, store=store)
    sources_text = build_sources_text([s.name for s in scrapers])
    tg_handler = TelegramHandler(store, search_fn=search_fn, sources_text=sources_text,
                                 contact_fn=get_contact, health=health)

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
        args=(scrapers, store, notifier, stop_event, poll_interval, fast_interval, health),
        daemon=True,
        name="scraping",
    )
    t_telegram = threading.Thread(
        target=telegram_thread,
        args=(tg_handler, stop_event, health),
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
        health.schreiben(erzwingen=True)
        logger.info("Bot beendet.")


if __name__ == "__main__":
    main()
