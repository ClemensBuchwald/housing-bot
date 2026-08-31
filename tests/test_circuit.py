"""Stromkreis-Sicherung gegen Aufrufstürme.

Ohne sie führt ein Ausfall zu einem Aufruf je Inserat: Bei abgelaufenem
Schlüssel und 150 Inseraten aus einer Quelle sind das 150 zwecklose Anfragen —
alle zwei Minuten aufs Neue.
"""
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import src.main as M
from src.llm.circuit import (
    GESCHLOSSEN,
    HALB_OFFEN,
    OFFEN,
    CircuitBreaker,
    CircuitProvider,
)
from src.llm.errors import (
    LLMAuthError,
    LLMConfigError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMTemporaryError,
)
from src.store import EVAL_BLOCKED, EVAL_PENDING, Store
from tests.fakes import FakeProvider, benutze_provider, bewertung
from tests.test_silent_failure import FakeNotifier, FakeScraper, _budgets_frei, _mandate
from src.models import Listing


def _provider(verhalten, **kw):
    b = CircuitBreaker(**kw)
    return CircuitProvider(FakeProvider(verhalten), b), b


# --- Öffnen -----------------------------------------------------------------

def test_auth_fehler_oeffnet_sofort_und_dauerhaft():
    p, b = _provider([LLMAuthError("401")])
    assert b.zustand() == GESCHLOSSEN
    with pytest.raises(LLMAuthError):
        p.complete(messages=[])
    assert b.zustand() == OFFEN
    assert b.info()["dauerhaft"] is True


def test_konfigurationsfehler_oeffnet_ebenfalls_dauerhaft():
    p, b = _provider([LLMConfigError("Modell unbekannt")])
    with pytest.raises(LLMConfigError):
        p.complete(messages=[])
    assert b.info()["dauerhaft"] is True


def test_einzelner_netzfehler_oeffnet_nicht():
    """Ein Aussetzer ist normal — dafür wird nicht gleich alles gesperrt."""
    p, b = _provider([LLMTemporaryError("weg"), bewertung()], schwelle=4)
    with pytest.raises(LLMTemporaryError):
        p.complete(messages=[])
    assert b.zustand() == GESCHLOSSEN


def test_fehlerserie_oeffnet():
    p, b = _provider([LLMTemporaryError("weg")], schwelle=3, cooldown_s=60)
    for _ in range(3):
        with pytest.raises(LLMTemporaryError):
            p.complete(messages=[])
    assert b.zustand() == OFFEN


def test_protokollfehler_zaehlt_nicht_auf_die_serie():
    """Ein unbrauchbares JSON ist eine Eigenschaft der Antwort, kein Ausfall."""
    p, b = _provider([LLMProtocolError("kaputt")], schwelle=3)
    for _ in range(10):
        with pytest.raises(LLMProtocolError):
            p.complete(messages=[])
    assert b.zustand() == GESCHLOSSEN
    assert b.info()["fehlerserie"] == 0


def test_erfolg_setzt_die_serie_zurueck():
    p, b = _provider([LLMTemporaryError("weg"), LLMTemporaryError("weg"),
                      bewertung(), LLMTemporaryError("weg")], schwelle=3)
    for erwartet_fehler in (True, True, False, True):
        try:
            p.complete(messages=[])
        except LLMTemporaryError:
            pass
    assert b.zustand() == GESCHLOSSEN, "der Erfolg dazwischen hat die Serie gebrochen"


# --- Aufrufsturm verhindern -------------------------------------------------

def test_offener_stromkreis_verhindert_weitere_aufrufe():
    inner = FakeProvider([LLMAuthError("401")])
    p = CircuitProvider(inner, CircuitBreaker())
    for _ in range(150):
        with pytest.raises(LLMAuthError):
            p.complete(messages=[])
    assert inner.anzahl_aufrufe == 1, (
        f"nach dem ersten Fehler darf kein Aufruf mehr rausgehen, "
        f"waren aber {inner.anzahl_aufrufe}"
    )
    assert p.breaker.info()["unterdrueckte_aufrufe"] == 149


def test_rate_limit_sperrt_fuer_die_genannte_zeit():
    inner = FakeProvider([LLMRateLimitError("429", retry_after=120)])
    p = CircuitProvider(inner, CircuitBreaker())
    for _ in range(20):
        with pytest.raises(LLMRateLimitError):
            p.complete(messages=[])
    assert inner.anzahl_aufrufe == 1
    assert 100 < p.breaker.info()["gesperrt_noch_s"] <= 120


def test_gesperrter_stromkreis_reicht_die_restzeit_weiter():
    """Damit der Warteschlangeneintrag nicht vor Ablauf der Sperre fällig wird."""
    inner = FakeProvider([LLMRateLimitError("429", retry_after=200)])
    p = CircuitProvider(inner, CircuitBreaker())
    with pytest.raises(LLMRateLimitError):
        p.complete(messages=[])
    try:
        p.complete(messages=[])
    except LLMRateLimitError as e:
        assert e.retry_after is not None and 150 < e.retry_after <= 200


def test_ganzer_zyklus_verbrennt_nur_einen_aufruf():
    """Der eigentliche Zweck: 150 Inserate, ein Ausfall, ein Anruf."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listings = [Listing(id=f"L{i}", portal="is24", url="u", titel=f"W{i}",
                            stadt="Berlin", stadtteil="Charlottenburg",
                            warmmiete=1300.0, flaeche=75.0, zimmer=3.0)
                    for i in range(60)]

        inner = FakeProvider([LLMAuthError("401 invalid api key")])
        with benutze_provider(CircuitProvider(inner, CircuitBreaker())), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper(listings)], store, FakeNotifier(), _mandate())

        assert inner.anzahl_aufrufe == 1, \
            f"es haetten {inner.anzahl_aufrufe} Anfragen rausgehen sollen: 1"
        # Trotzdem ist kein Inserat verloren und keines faelschlich abgelehnt.
        assert store._read("SELECT * FROM match_log") == []
        assert store.eval_queue_groesse(EVAL_BLOCKED) > 0
        store.close()


def test_stromkreis_markiert_nichts_als_abgelehnt():
    """Ein unterdrückter Aufruf darf kein fachliches Urteil erzeugen."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listings = [Listing(id=f"L{i}", portal="is24", url="u", titel="W",
                            stadt="Berlin", stadtteil="Charlottenburg",
                            warmmiete=1300.0, flaeche=75.0, zimmer=3.0)
                    for i in range(10)]
        with benutze_provider(CircuitProvider(FakeProvider([LLMAuthError("401")]),
                                              CircuitBreaker())), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper(listings)], store, FakeNotifier(), _mandate())

        assert store._read("SELECT * FROM match_log WHERE bestanden = 0") == []
        offen = store.eval_queue_zaehler()
        assert sum(offen.values()) == 10, "alle bleiben nachvollziehbar erhalten"
        store.close()


# --- Halb offen und Erholung ------------------------------------------------

def test_nach_abkuehlung_genau_ein_testaufruf():
    inner = FakeProvider([LLMTemporaryError("weg")])
    b = CircuitBreaker(schwelle=2, cooldown_s=0.05)
    p = CircuitProvider(inner, b)

    for _ in range(2):
        with pytest.raises(LLMTemporaryError):
            p.complete(messages=[])
    assert b.zustand() == OFFEN
    aufrufe_vorher = inner.anzahl_aufrufe

    time.sleep(0.08)
    assert b.zustand() == HALB_OFFEN

    # Erster Versuch geht durch, alle weiteren nicht
    with pytest.raises(LLMTemporaryError):
        p.complete(messages=[])
    assert inner.anzahl_aufrufe == aufrufe_vorher + 1


def test_erfolgreicher_testaufruf_schliesst_wieder():
    inner = FakeProvider([LLMTemporaryError("weg"), LLMTemporaryError("weg"),
                          bewertung(passt=True)])
    b = CircuitBreaker(schwelle=2, cooldown_s=0.05)
    p = CircuitProvider(inner, b)

    for _ in range(2):
        with pytest.raises(LLMTemporaryError):
            p.complete(messages=[])
    assert b.zustand() == OFFEN

    time.sleep(0.08)
    ergebnis = p.complete(messages=[])          # der Testaufruf gelingt
    assert ergebnis.stop_reason == "end_turn"
    assert b.zustand() == GESCHLOSSEN
    assert b.info()["unterdrueckte_aufrufe"] == 0


def test_gescheiterter_testaufruf_verlaengert_die_sperre():
    inner = FakeProvider([LLMTemporaryError("weg")])
    b = CircuitBreaker(schwelle=2, cooldown_s=0.05)
    p = CircuitProvider(inner, b)
    for _ in range(2):
        with pytest.raises(LLMTemporaryError):
            p.complete(messages=[])

    time.sleep(0.08)
    with pytest.raises(LLMTemporaryError):
        p.complete(messages=[])                 # Testaufruf scheitert
    assert b.zustand() == OFFEN
    # Die Abkühlphase hat sich verdoppelt
    time.sleep(0.06)
    assert b.zustand() == OFFEN, "nach dem Fehlschlag wird laenger gewartet"


def test_auth_bleibt_auch_nach_wartezeit_gesperrt():
    """Auth heilt nicht durch Warten — nur durch einen Neustart mit neuer Konfiguration."""
    inner = FakeProvider([LLMAuthError("401")])
    b = CircuitBreaker(schwelle=2, cooldown_s=0.01)
    p = CircuitProvider(inner, b)
    with pytest.raises(LLMAuthError):
        p.complete(messages=[])
    time.sleep(0.05)
    assert b.zustand() == OFFEN
    with pytest.raises(LLMAuthError):
        p.complete(messages=[])
    assert inner.anzahl_aufrufe == 1

    b.reset()                                    # entspricht dem Prozessneustart
    assert b.zustand() == GESCHLOSSEN


# --- Zusammenspiel mit der Warteschlange ------------------------------------

def test_unterdrueckter_aufruf_behaelt_die_fehlersemantik():
    """Der Stromkreis wirft denselben Fehlertyp — die Warteschlange soll ihn
    genauso behandeln wie den echten."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listings = [Listing(id=f"L{i}", portal="is24", url="u", titel="W",
                            stadt="Berlin", stadtteil="Charlottenburg",
                            warmmiete=1300.0, flaeche=75.0, zimmer=3.0)
                    for i in range(5)]
        with benutze_provider(CircuitProvider(FakeProvider([LLMAuthError("401")]),
                                              CircuitBreaker())), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper(listings)], store, FakeNotifier(), _mandate())

        for i in range(5):
            e = store.get_eval_queue_entry(f"L{i}", "is24")
            assert e["status"] == EVAL_BLOCKED
            assert e["retry_count"] == 0, "auch unterdrueckt kostet Auth keinen Versuch"
        store.close()


def test_ratelimit_sperre_erzeugt_pending_mit_wartezeit():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listings = [Listing(id=f"L{i}", portal="is24", url="u", titel="W",
                            stadt="Berlin", stadtteil="Charlottenburg",
                            warmmiete=1300.0, flaeche=75.0, zimmer=3.0)
                    for i in range(4)]
        with benutze_provider(CircuitProvider(
                FakeProvider([LLMRateLimitError("429", retry_after=90)]),
                CircuitBreaker())), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper(listings)], store, FakeNotifier(), _mandate())

        for i in range(4):
            e = store.get_eval_queue_entry(f"L{i}", "is24")
            assert e["status"] == EVAL_PENDING
            assert e["next_retry_at"] is not None
        store.close()


def test_budget_wird_auch_bei_fehlern_verbraucht():
    """Sonst liefe eine Dauerstörung ungebremst durch alle Inserate."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listings = [Listing(id=f"L{i}", portal="is24", url="u", titel="W",
                            stadt="Berlin", stadtteil="Charlottenburg",
                            warmmiete=1300.0, flaeche=75.0, zimmer=3.0)
                    for i in range(80)]
        with benutze_provider(CircuitProvider(FakeProvider([LLMAuthError("401")]),
                                              CircuitBreaker())), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper(listings)], store, FakeNotifier(), _mandate())

        assert M.EVAL_BUDGET.verbraucht() > 0, "Fehlversuche muessen mitzaehlen"
        assert M.EVAL_BUDGET.verbraucht() <= M.MAX_EVAL_PRO_STUNDE
        store.close()


def test_info_enthaelt_keine_geheimnisse():
    p, b = _provider([LLMAuthError("sk-ant-geheim-darf-nicht-auftauchen")])
    with pytest.raises(LLMAuthError):
        p.complete(messages=[])
    text = repr(b.info())
    assert "sk-ant" not in text
    assert set(b.info()) == {"zustand", "kategorie", "dauerhaft", "fehlerserie",
                             "gesperrt_noch_s", "unterdrueckte_aufrufe"}
