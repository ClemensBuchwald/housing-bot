"""Automatische Wiedervorlage gescheiterter Bewertungen.

Zwei Grundsätze prägen den Entwurf:

  Neue Inserate zuerst. Ein Retry betrifft etwas, das wir schon gesehen haben;
  ein neues Inserat kann in Minuten weg sein. Die Wiedervorlage läuft deshalb
  am Ende des Zyklus mit dem, was an Budget übrig blieb.

  Neueste zuerst (LIFO). Wohnungsangebote sind verderbliche Ware — bei Rückstau
  nützt eine Meldung nur noch beim jüngsten Inserat.
"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import src.main as M
from src.llm.errors import (
    LLMAuthError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMTemporaryError,
)
from src.models import Listing
from src.store import EVAL_BLOCKED, EVAL_EXPIRED, EVAL_PENDING, Store
from tests.fakes import FakeProvider, benutze_provider, bewertung
from tests.test_silent_failure import (
    FakeNotifier,
    FakeScraper,
    _budgets_frei,
    _listing,
    _mandate,
)


def _ablegen(store, lid, minuten_alt=0, portal="is24"):
    """Inserat als gescheiterte Bewertung einstellen, mit steuerbarem Fundalter."""
    l = Listing(id=lid, portal=portal, url="u", titel=f"Wohnung {lid}", stadt="Berlin",
                stadtteil="Charlottenburg", warmmiete=1400.0, flaeche=80.0, zimmer=3.0)
    store.claim(l)
    if minuten_alt:
        gefunden = (datetime.now() - timedelta(minutes=minuten_alt)).isoformat()
        with store._tx() as c:
            c.execute("UPDATE listings SET seen_at = ? WHERE id = ? AND portal = ?",
                      (gefunden, lid, portal))
    store.enqueue_eval(lid, portal, 7, "Netz weg", verbraucht_versuch=True,
                       max_versuche=3, retryable=True)
    return l


# --- Abarbeitung ------------------------------------------------------------

def test_offene_bewertung_wird_automatisch_nachgeholt():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "L1")
        assert store.eval_queue_groesse() == 1

        with benutze_provider(FakeProvider([bewertung(passt=True, score=90)])):
            assert M._retries_abarbeiten(store, _mandate()) == 1

        assert store.eval_queue_groesse() == 0, "erledigt heisst weg"
        offen = store.get_pending_matches()
        assert len(offen) == 1 and offen[0]["score"] == 90
        store.close()


def test_nachbewerteter_treffer_geht_ueber_den_normalen_versandweg():
    """Die Wiedervorlage verschickt selbst nichts — sie schreibt die Bewertung,
    _nachholen stellt zu. So gibt es nur EINEN Versandweg."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listing = _ablegen(store, "L1")
        notifier = FakeNotifier()

        with benutze_provider(FakeProvider([bewertung(passt=True, score=90)])), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper([])], store, notifier, _mandate())

        assert notifier.gesendet == [listing.id], "genau einmal gemeldet"
        store.close()


def test_fachliche_ablehnung_beendet_die_wiedervorlage():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "L1")

        with benutze_provider(FakeProvider([bewertung(passt=False, score=5)])):
            M._retries_abarbeiten(store, _mandate())

        assert store.eval_queue_groesse() == 0, "eine Ablehnung ist eine Entscheidung"
        assert store.get_pending_matches() == []
        eintraege = store._read("SELECT bestanden FROM match_log")
        assert [r["bestanden"] for r in eintraege] == [0], "jetzt eine ECHTE Ablehnung"
        store.close()


# --- Reihenfolge: LIFO ------------------------------------------------------

def test_neueste_zuerst():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        for i in range(8):
            _ablegen(store, f"L{i}")
            # created_at kommt aus enqueue_eval; künstlich staffeln
            with store._tx() as c:
                c.execute("UPDATE eval_queue SET created_at = ? WHERE listing_id = ?",
                          (f"2026-08-31T10:0{i}:00", f"L{i}"))

        reihenfolge = [r["listing_id"] for r in store.get_due_evaluations(limit=8)]
        assert reihenfolge == ["L7", "L6", "L5", "L4", "L3", "L2", "L1", "L0"], \
            f"LIFO verletzt: {reihenfolge}"
        store.close()


def test_bei_rueckstau_kommen_die_juengsten_dran():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        for i in range(10):
            _ablegen(store, f"L{i}")
            with store._tx() as c:
                c.execute("UPDATE eval_queue SET created_at = ? WHERE listing_id = ?",
                          (f"2026-08-31T10:{i:02d}:00", f"L{i}"))

        with benutze_provider(FakeProvider([bewertung(passt=True, score=80)])):
            M._retries_abarbeiten(store, _mandate())

        bewertet = {r["listing_id"] for r in store._read("SELECT listing_id FROM match_log")}
        assert bewertet == {"L9", "L8", "L7", "L6", "L5"}, \
            f"es muessen die 5 juengsten sein, war: {bewertet}"
        store.close()


# --- TTL --------------------------------------------------------------------

def test_zu_altes_inserat_wird_nicht_mehr_gemeldet():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "ALT", minuten_alt=90)

        with benutze_provider(FakeProvider([bewertung(passt=True, score=95)])) as p:
            M._retries_abarbeiten(store, _mandate())
            assert p.anzahl_aufrufe == 0, "abgelaufen heisst: gar nicht erst bewerten"

        eintrag = store.get_eval_queue_entry("ALT", "is24")
        assert eintrag["status"] == EVAL_EXPIRED
        assert store.get_pending_matches() == [], "keine verspaetete Meldung"
        store.close()


def test_frisches_inserat_laeuft_nicht_ab():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "NEU", minuten_alt=10)
        with benutze_provider(FakeProvider([bewertung(passt=True, score=95)])):
            assert M._retries_abarbeiten(store, _mandate()) == 1
        store.close()


def test_ttl_grenze():
    jetzt = datetime.now()
    assert M._ist_abgelaufen((jetzt - timedelta(minutes=61)).isoformat()) is True
    assert M._ist_abgelaufen((jetzt - timedelta(minutes=59)).isoformat()) is False
    assert M._ist_abgelaufen(None) is False, "ohne Zeitangabe nicht verwerfen"
    assert M._ist_abgelaufen("kaputter Zeitstempel") is False


def test_abgelaufene_belegen_kein_budget():
    """Verfallene Eintraege duerfen die 5 Retry-Plaetze nicht verbrauchen."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        for i in range(6):
            _ablegen(store, f"ALT{i}", minuten_alt=120)
        _ablegen(store, "FRISCH", minuten_alt=5)

        with benutze_provider(FakeProvider([bewertung(passt=True, score=80)])):
            M._retries_abarbeiten(store, _mandate())

        bewertet = [r["listing_id"] for r in store._read("SELECT listing_id FROM match_log")]
        assert bewertet == ["FRISCH"]
        store.close()


# --- Mengenbegrenzung -------------------------------------------------------

def test_hoechstens_fuenf_wiedervorlagen_pro_zyklus():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        for i in range(12):
            _ablegen(store, f"L{i}")

        with benutze_provider(FakeProvider([bewertung(passt=True, score=80)])) as p:
            anzahl = M._retries_abarbeiten(store, _mandate())

        assert anzahl == M.MAX_RETRY_PRO_ZYKLUS == 5
        assert p.anzahl_aufrufe == 5
        assert store.eval_queue_groesse() == 7, "der Rest bleibt liegen"
        store.close()


def test_stundenbudget_bremst_die_wiedervorlage():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        for i in range(30):
            _ablegen(store, f"L{i}")

        gesamt = 0
        with benutze_provider(FakeProvider([bewertung(passt=True, score=80)])):
            for _ in range(10):          # zehn Zyklen hintereinander
                gesamt += M._retries_abarbeiten(store, _mandate())

        assert gesamt == M.MAX_RETRY_PRO_STUNDE == 15, \
            f"Stundenbudget nicht eingehalten: {gesamt}"
        store.close()


def test_neue_inserate_haben_vorrang_vor_wiedervorlage():
    """Verbraucht der reguläre Durchlauf das Bewertungsbudget, bleibt für
    Retries nichts übrig — genau so ist es gewollt."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "ALTFALL")

        neue = [Listing(id=f"N{i}", portal="is24", url="u", titel=f"Neu {i}",
                        stadt="Berlin", stadtteil="Charlottenburg",
                        warmmiete=1300.0, flaeche=75.0, zimmer=3.0) for i in range(3)]

        # Bewertungsbudget bis auf zwei Plätze belegen
        M.EVAL_BUDGET._zeiten = [M.time.time()] * (M.MAX_EVAL_PRO_STUNDE - 3)

        with benutze_provider(FakeProvider([bewertung(passt=True, score=80)])), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper(neue)], store, FakeNotifier(), _mandate())

        bewertet = {r["listing_id"] for r in store._read("SELECT listing_id FROM match_log")}
        assert bewertet == {"N0", "N1", "N2"}, "die neuen Inserate zuerst"
        assert "ALTFALL" not in bewertet, "der Altfall wartet auf freies Budget"
        assert store.get_eval_queue_entry("ALTFALL", "is24") is not None
        store.close()


def test_wiedervorlage_laeuft_erst_nach_den_quellen():
    """Die Reihenfolge im Zyklus ist Teil der Zusage 'Neue zuerst'."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "ALTFALL")
        ablauf = []

        class Beobachtet(FakeScraper):
            def fetch_listings(self, criteria):
                ablauf.append("quelle")
                return []

        echte_retries = M._retries_abarbeiten

        def beobachtet_retries(store_, mandate_):
            ablauf.append("wiedervorlage")
            return echte_retries(store_, mandate_)

        with benutze_provider(FakeProvider([bewertung(passt=True, score=80)])), \
             patch.object(M, "_retries_abarbeiten", beobachtet_retries):
            M.run_scraping_cycle([Beobachtet([]), Beobachtet([])], store,
                                 FakeNotifier(), _mandate())

        assert ablauf == ["quelle", "quelle", "wiedervorlage"]
        store.close()


# --- Fehlersemantik bleibt erhalten ----------------------------------------

def test_auth_fehler_im_retry_blockiert_wieder():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "L1")
        vorher = store.get_eval_queue_entry("L1", "is24")["retry_count"]

        with benutze_provider(FakeProvider([LLMAuthError("401")])):
            M._retries_abarbeiten(store, _mandate())

        eintrag = store.get_eval_queue_entry("L1", "is24")
        assert eintrag["status"] == EVAL_BLOCKED
        assert eintrag["retry_count"] == vorher, "Auth verbraucht keinen Versuch"
        assert store._read("SELECT * FROM match_log") == []
        store.close()


def test_protokollfehler_im_retry_endet_nach_einer_wiederholung():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        l = Listing(id="P1", portal="is24", url="u", titel="X", stadt="Berlin",
                    stadtteil="Charlottenburg", warmmiete=1200.0, flaeche=70.0, zimmer=2.0)
        store.claim(l)
        store.enqueue_eval("P1", "is24", 7, "kaputt", verbraucht_versuch=True,
                           max_versuche=LLMProtocolError.max_versuche, retryable=True)
        assert store.get_eval_queue_entry("P1", "is24")["status"] == EVAL_PENDING

        with benutze_provider(FakeProvider([LLMProtocolError("wieder kaputt")])):
            M._retries_abarbeiten(store, _mandate())

        e = store.get_eval_queue_entry("P1", "is24")
        assert e["retry_count"] == 2 and e["status"] == "failed", \
            "nach genau einer Wiederholung ist Schluss"
        store.close()


def test_temporaerer_fehler_staffelt_den_abstand():
    fehler = LLMTemporaryError("weg")
    abstaende = []
    for versuch in (1, 2, 3):
        ziel = datetime.fromisoformat(M._naechster_versuch(fehler, versuch))
        abstaende.append(round((ziel - datetime.now()).total_seconds()))
    assert abstaende[0] < abstaende[1] < abstaende[2], f"kein Anstieg: {abstaende}"
    assert abstaende[0] == pytest.approx(120, abs=3)
    assert abstaende[1] == pytest.approx(240, abs=3)


def test_backoff_bleibt_gedeckelt():
    fehler = LLMTemporaryError("weg")
    ziel = datetime.fromisoformat(M._naechster_versuch(fehler, 20))
    assert (ziel - datetime.now()).total_seconds() <= M._RETRY_DECKEL_S + 3


def test_ratelimit_haelt_sich_an_retry_after():
    fehler = LLMRateLimitError("429", retry_after=300)
    ziel = datetime.fromisoformat(M._naechster_versuch(fehler, 3))
    wartezeit = (ziel - datetime.now()).total_seconds()
    assert wartezeit == pytest.approx(300, abs=3), \
        "die Angabe der Gegenseite hat Vorrang vor der eigenen Staffelung"


def test_wartezeit_wird_respektiert():
    """Ein Eintrag mit Zukunfts-Termin darf nicht vorzeitig drankommen."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "L1")
        zukunft = (datetime.now() + timedelta(minutes=30)).isoformat()
        with store._tx() as c:
            c.execute("UPDATE eval_queue SET next_retry_at = ?", (zukunft,))

        with benutze_provider(FakeProvider([bewertung()])) as p:
            assert M._retries_abarbeiten(store, _mandate()) == 0
            assert p.anzahl_aufrufe == 0
        store.close()


def test_bereits_gemeldetes_inserat_wird_nur_aufgeraeumt():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "L1")
        store.mark_notified("L1", "is24")

        with benutze_provider(FakeProvider([bewertung()])) as p:
            M._retries_abarbeiten(store, _mandate())
            assert p.anzahl_aufrufe == 0, "nichts mehr offen — kein Token verbrennen"
        assert store.eval_queue_groesse() == 0
        store.close()


# --- Freigabe blockierter Einträge -----------------------------------------

def test_start_gibt_blockierte_eintraege_frei():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        _ablegen(store, "L1")
        with benutze_provider(FakeProvider([LLMAuthError("401")])):
            M._retries_abarbeiten(store, _mandate())
        assert store.eval_queue_groesse(EVAL_BLOCKED) == 1

        # Neustart nach Schlüsselkorrektur
        store.close()
        store2 = Store(Path(d) / "t.db")
        assert store2.reaktiviere_blockierte() == 1

        _budgets_frei()
        with benutze_provider(FakeProvider([bewertung(passt=True, score=88)])):
            assert M._retries_abarbeiten(store2, _mandate()) == 1
        assert store2.eval_queue_groesse() == 0
        store2.close()


def test_freigabe_ohne_behobene_ursache_blockiert_sofort_wieder():
    """Kein Aufrufsturm: Besteht der Fehler fort, ist der Eintrag nach EINEM
    Versuch wieder blockiert — und hat dabei keinen Versuch verbraucht."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        for i in range(5):
            _ablegen(store, f"L{i}")

        with benutze_provider(FakeProvider([LLMAuthError("401")])) as p:
            M._retries_abarbeiten(store, _mandate())
        assert store.eval_queue_groesse(EVAL_BLOCKED) == 5

        store.reaktiviere_blockierte()
        _budgets_frei()
        with benutze_provider(FakeProvider([LLMAuthError("401")])) as p:
            M._retries_abarbeiten(store, _mandate())
            assert p.anzahl_aufrufe <= M.MAX_RETRY_PRO_ZYKLUS

        assert store.eval_queue_groesse(EVAL_BLOCKED) == 5
        for i in range(5):
            assert store.get_eval_queue_entry(f"L{i}", "is24")["retry_count"] == 1, \
                "der ursprüngliche Netzfehler zählte, der Auth-Fehler nicht"
        store.close()
