"""Warteschlange für technisch gescheiterte Bewertungen.

Sie ist der einzige Rückweg für ein Inserat, das durch claim() schon als
gesehen gilt, aber nie fachlich beurteilt wurde.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import src.main as M
from src.llm.errors import (
    LLMAuthError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMTemporaryError,
)
from src.models import Listing
from src.store import EVAL_BLOCKED, EVAL_FAILED, EVAL_PENDING, Store
from tests.fakes import FakeProvider, benutze_provider, bewertung
from tests.test_silent_failure import (
    FakeNotifier,
    FakeScraper,
    _budgets_frei,
    _listing,
    _mandate,
)


def _zyklus(store, listing, fehler, notifier=None):
    """Ein Scraping-Zyklus, bei dem die Bewertung mit ``fehler`` scheitert."""
    _budgets_frei()
    with benutze_provider(FakeProvider([fehler])), \
         patch.object(M, "enrich_listing", lambda l: False):
        M.run_scraping_cycle([FakeScraper([listing])], store, notifier or FakeNotifier(), _mandate())


# --- Fall 3: genau ein Eintrag je Fehlerfall -------------------------------

def test_fehler_erzeugt_genau_einen_eintrag():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        _zyklus(store, listing, LLMTemporaryError("weg"))

        assert store.eval_queue_groesse() == 1
        rows = store._read("SELECT * FROM eval_queue")
        assert len(rows) == 1
        assert rows[0]["listing_id"] == listing.id
        assert rows[0]["portal"] == listing.portal
        assert rows[0]["mandate_id"] == 7
        assert rows[0]["last_error"], "Fehlertext muss festgehalten werden"
        assert rows[0]["created_at"] and rows[0]["updated_at"]
        store.close()


# --- Fall 4: Wiederholung erzeugt keine Dubletten --------------------------

def test_wiederholter_fehler_erzeugt_keine_dublette():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()

        for _ in range(4):
            # claim() greift ab dem zweiten Lauf, deshalb direkt einstellen
            store.enqueue_eval(listing.id, listing.portal, 7, "Netz weg",
                               verbraucht_versuch=True, max_versuche=3, retryable=True)

        assert store.eval_queue_groesse() == 1, "Primaerschluessel muss Dubletten verhindern"
        eintrag = store.get_eval_queue_entry(listing.id, listing.portal)
        assert eintrag["retry_count"] == 4, "der Zaehler laeuft weiter, die Zeile bleibt eine"
        store.close()


def test_verschiedene_portale_sind_getrennte_eintraege():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        store.enqueue_eval("gleiche-id", "is24", 1, "x")
        store.enqueue_eval("gleiche-id", "immowelt", 1, "x")
        assert store.eval_queue_groesse() == 2
        store.close()


# --- Fall 7: Auth-Fehler verbraucht keinen Versuch -------------------------

def test_auth_fehler_verbraucht_keinen_versuch():
    """Ein ungueltiger Schluessel heilt nicht durch Warten, sondern durch einen
    Eingriff. Wuerde er zaehlen, waere das Kontingent aufgebraucht, bevor
    jemand den Schluessel erneuern konnte."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()

        for _ in range(10):
            store.enqueue_eval(
                listing.id, listing.portal, 7, "Schluessel ungueltig",
                verbraucht_versuch=LLMAuthError.verbraucht_versuch,
                max_versuche=LLMAuthError.max_versuche,
                retryable=LLMAuthError.retryable,
            )

        eintrag = store.get_eval_queue_entry(listing.id, listing.portal)
        assert eintrag["retry_count"] == 0, "Auth-Fehler darf das Kontingent nie anfassen"
        assert eintrag["status"] == EVAL_BLOCKED
        assert eintrag["next_retry_at"] is None, "Warten hilft hier nicht"
        store.close()


def test_auth_fehler_im_echten_zyklus():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        _zyklus(store, listing, LLMAuthError("401"))

        eintrag = store.get_eval_queue_entry(listing.id, listing.portal)
        assert eintrag["retry_count"] == 0
        assert eintrag["status"] == EVAL_BLOCKED
        assert store._read("SELECT * FROM match_log") == []
        store.close()


def test_ratelimit_verbraucht_versuch_und_wartet():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        _zyklus(store, listing, LLMRateLimitError("429", retry_after=45))

        eintrag = store.get_eval_queue_entry(listing.id, listing.portal)
        assert eintrag["retry_count"] == 1
        assert eintrag["status"] == EVAL_PENDING
        assert eintrag["next_retry_at"] is not None, "Backoff muss vermerkt sein"
        store.close()


def test_retry_after_wird_gedeckelt():
    """Ein grosszuegiges Retry-After der Gegenseite darf einen Eintrag nicht
    auf Stunden hinausschieben."""
    from datetime import datetime
    fehler = LLMRateLimitError("429", retry_after=100000)
    ziel = datetime.fromisoformat(M._naechster_versuch(fehler))
    wartezeit = (ziel - datetime.now()).total_seconds()
    assert wartezeit <= M._RETRY_DECKEL_S + 5, f"Deckel greift nicht: {wartezeit}s"


# --- Fall 8: Protokollfehler hoechstens einmal wiederholbar ----------------

def test_protokollfehler_hoechstens_einmal_wiederholbar():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()

        def melden():
            store.enqueue_eval(
                listing.id, listing.portal, 7, "kaputtes JSON",
                verbraucht_versuch=LLMProtocolError.verbraucht_versuch,
                max_versuche=LLMProtocolError.max_versuche,
                retryable=LLMProtocolError.retryable,
            )

        melden()
        e = store.get_eval_queue_entry(listing.id, listing.portal)
        assert e["status"] == EVAL_PENDING and e["retry_count"] == 1, \
            "nach dem ersten Fehlschlag ist genau eine Wiederholung offen"

        melden()
        e = store.get_eval_queue_entry(listing.id, listing.portal)
        assert e["status"] == EVAL_FAILED and e["retry_count"] == 2, \
            "nach der einen Wiederholung ist Schluss"

        assert store.get_due_evaluations(jetzt="2999-01-01T00:00:00") == [], \
            "abgeschlossene Faelle duerfen nicht mehr faellig werden"
        store.close()


def test_temporaerer_fehler_erlaubt_drei_wiederholungen():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        zustaende = []
        for _ in range(5):
            store.enqueue_eval("L", "is24", 1, "weg",
                               verbraucht_versuch=True,
                               max_versuche=LLMTemporaryError.max_versuche,
                               retryable=True)
            zustaende.append(store.get_eval_queue_entry("L", "is24")["status"])
        assert zustaende == [EVAL_PENDING, EVAL_PENDING, EVAL_PENDING, EVAL_FAILED, EVAL_FAILED]
        store.close()


# --- Fall 9: hoechstens eine Benachrichtigung ------------------------------

def test_spaetere_erfolgreiche_bewertung_meldet_hoechstens_einmal():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        notifier = FakeNotifier()

        # 1. Durchlauf: technischer Fehler -> nichts gemeldet, nichts entschieden
        _zyklus(store, listing, LLMTemporaryError("weg"), notifier)
        assert notifier.gesendet == []

        # 2. Durchlauf: dasselbe Inserat, jetzt beurteilt das Modell fachlich.
        #    claim() liefert False (schon bekannt), deshalb der direkte Weg.
        with benutze_provider(FakeProvider([bewertung(passt=True, score=90)])):
            from src.evaluator import evaluate_listing
            ev = evaluate_listing(listing, _mandate())
        store.save_evaluation(listing.id, listing.portal, 7, ev.__dict__)
        store.resolve_eval(listing.id, listing.portal)

        _budgets_frei()
        assert M._nachholen(store, notifier) == 1
        assert notifier.gesendet == [listing.id]

        # Jeder weitere Durchlauf darf nichts mehr schicken.
        _budgets_frei()
        assert M._nachholen(store, notifier) == 0
        assert notifier.gesendet == [listing.id], "keine zweite Meldung"
        store.close()


def test_mehrere_bewertungen_erzeugen_nur_eine_meldung():
    """match_log ist ein Verlaufsprotokoll: Eine Nachbewertung legt einen
    ZWEITEN Eintrag an. Ohne Entdopplung ginge das Inserat zweimal raus."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        store.claim(listing)

        store.save_evaluation(listing.id, listing.portal, 7, bewertung(passt=True, score=70))
        store.save_evaluation(listing.id, listing.portal, 7, bewertung(passt=True, score=90))
        assert len(store._read("SELECT * FROM match_log")) == 2, "Historie bleibt vollstaendig"

        offen = store.get_pending_matches()
        assert len(offen) == 1, "aber nur EIN offener Treffer"
        assert offen[0]["score"] == 90, "es zaehlt die juengste Bewertung"

        notifier = FakeNotifier()
        _budgets_frei()
        assert M._nachholen(store, notifier) == 1
        assert notifier.gesendet == [listing.id]
        store.close()


def test_juengste_ablehnung_schlaegt_alten_treffer():
    """Wird ein Inserat nachtraeglich als unpassend beurteilt, darf die alte
    positive Bewertung es nicht mehr in die Meldung heben."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        store.claim(listing)
        store.save_evaluation(listing.id, listing.portal, 7, bewertung(passt=True, score=90))
        store.save_evaluation(listing.id, listing.portal, 7, bewertung(passt=False, score=10))

        assert store.get_pending_matches() == []
        store.close()


def test_bereits_gemeldetes_inserat_wird_nicht_erneut_gemeldet():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        store.claim(listing)
        store.save_evaluation(listing.id, listing.portal, 7, bewertung(passt=True))
        store.mark_notified(listing.id, listing.portal)

        assert store.ist_gemeldet(listing.id, listing.portal) is True
        assert store.get_pending_matches() == []

        # Auch eine erneute Bewertung nach einem Retry aendert daran nichts.
        store.save_evaluation(listing.id, listing.portal, 7, bewertung(passt=True, score=99))
        assert store.get_pending_matches() == [], "ein Retry rechtfertigt keine zweite Meldung"
        store.close()


# --- Nachtraeglich gefundene Luecken ---------------------------------------

def test_warteschlangeneintrag_ist_immer_abarbeitbar():
    """get_due_evaluations verknuepft mit listings. Ein Eintrag ohne zugehoerigen
    Listing-Datensatz waere unsichtbar und damit wertlos."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing("nie-reserviert")
        assert not store.is_known(listing.id, listing.portal)

        M._bewertung_zurueckstellen(store, listing, 7, LLMTemporaryError("weg"))

        faellig = store.get_due_evaluations(jetzt="2999-01-01T00:00:00")
        assert [r["listing_id"] for r in faellig] == [listing.id], \
            "der Eintrag muss auch ohne vorheriges claim() auffindbar sein"
        assert faellig[0]["titel"] == listing.titel
        store.close()


def test_abgelehntes_inserat_verlaesst_die_warteschlange():
    """Auch eine Ablehnung ist eine Entscheidung — die Wiedervorlage ist damit
    erledigt und darf nicht ewig stehen bleiben."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing("abgelehnt")

        # Erster Anlauf scheitert technisch
        M._bewertung_zurueckstellen(store, listing, 7, LLMTemporaryError("weg"))
        assert store.eval_queue_groesse() == 1

        # Zweiter Anlauf: das Modell entscheidet — dagegen
        with benutze_provider(FakeProvider([bewertung(passt=False, score=5)])), \
             patch.object(M, "enrich_listing", lambda l: False), \
             patch.object(M, "load_criteria", lambda: {}), \
             patch.object(M, "IS24Scraper", lambda: FakeScraper([listing])), \
             patch.object(M, "ImmoweltScraper", lambda: FakeScraper([], "immowelt")), \
             patch.object(M, "VonoviaScraper", lambda: FakeScraper([], "vonovia")), \
             patch.object(M, "CharlotteScraper", lambda: FakeScraper([], "charlotte")), \
             patch.object(M, "WBMScraper", lambda: FakeScraper([], "wbm")), \
             patch.object(M, "GESOBAUScraper", lambda: FakeScraper([], "gesobau")), \
             patch.object(M, "GewobagScraper", lambda: FakeScraper([], "gewobag")), \
             patch.object(M, "KleinanzeigenScraper", lambda: FakeScraper([], "kleinanzeigen")), \
             patch.object(M, "InBerlinWohnenScraper", lambda page_limit=4: FakeScraper([], "inberlinwohnen")):
            treffer = M.run_one_off_search({}, _mandate(), store=store, include_seen=True)

        assert treffer == [], "score 5 ist kein Treffer"
        assert store.eval_queue_groesse() == 0, \
            "nach der fachlichen Entscheidung ist die Wiedervorlage erledigt"
        store.close()


def test_blockierte_eintraege_sind_keine_sackgasse():
    """Nach einem Auth-Ausfall stehen die Inserate auf 'blocked'. Ohne Rueckweg
    waeren sie genauso verloren, wie es die Warteschlange verhindern soll."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        listing = _listing()
        _zyklus(store, listing, LLMAuthError("401"))

        assert store.eval_queue_groesse(EVAL_BLOCKED) == 1
        assert store.get_due_evaluations(jetzt="2999-01-01T00:00:00") == [], \
            "solange die Ursache besteht, wird nicht erneut versucht"

        # Schluessel erneuert -> Freigabe
        assert store.reaktiviere_blockierte() == 1
        faellig = store.get_due_evaluations()
        assert [r["listing_id"] for r in faellig] == [listing.id]
        assert faellig[0]["retry_count"] == 0, "der Auth-Fehler hat nie einen Versuch gekostet"
        store.close()


def test_freigabe_ruehrt_erschoepfte_eintraege_nicht_an():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        store.enqueue_eval("a", "is24", 1, "auth", verbraucht_versuch=False,
                           max_versuche=0, retryable=False)
        for _ in range(5):
            store.enqueue_eval("b", "is24", 1, "kaputt", verbraucht_versuch=True,
                               max_versuche=1, retryable=True)

        assert store.get_eval_queue_entry("b", "is24")["status"] == EVAL_FAILED
        assert store.reaktiviere_blockierte() == 1, "nur der blockierte Eintrag"
        assert store.get_eval_queue_entry("b", "is24")["status"] == EVAL_FAILED
        store.close()
