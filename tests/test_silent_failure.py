"""Kernregel: Ein technischer Fehler ist keine fachliche Entscheidung.

Vorher wurde jede Störung — abgelaufener Schlüssel, Zeitüberschreitung,
abgeschnittenes JSON — zu ``passt=False`` mit Score 0 und landete als
endgültiges "abgelehnt" in der Datenbank. Diese Tests halten fest, dass das
nicht mehr passieren kann.
"""
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import src.main as M
from src.evaluator import Evaluation, evaluate_listing, parse_mandate
from src.llm.errors import (
    LLMAuthError,
    LLMError,
    LLMProtocolError,
    LLMRateLimitError,
    LLMTemporaryError,
)
from src.models import Listing
from src.store import EVAL_PENDING, Store
from tests.fakes import FakeProvider, abgeschnitten, benutze_provider, bewertung


class FakeScraper:
    def __init__(self, listings, name="is24"):
        self.name = name
        self._listings = listings

    def fetch_listings(self, criteria):
        return list(self._listings)


class FakeNotifier:
    def __init__(self):
        self.gesendet = []
        self.texte = []

    def send_evaluation(self, listing, evaluation):
        self.gesendet.append(listing.id)
        return True

    def send_text(self, text, chat_id=None):
        self.texte.append(text)
        return True


def _listing(lid="L1") -> Listing:
    return Listing(
        id=lid, portal="is24", url="https://example.invalid/x", titel="3 Zi Altbau",
        stadt="Berlin", stadtteil="Charlottenburg",
        kaltmiete=1200.0, warmmiete=1500.0, flaeche=90.0, zimmer=3.0,
    )


def _mandate():
    return {"id": 7, "raw_text": "3 Zimmer Charlottenburg", "structured": {}}


def _budgets_frei():
    """Modulweite Stundenbudgets zwischen Tests zurücksetzen."""
    M.EVAL_BUDGET._zeiten = []
    M.ALERT_BUDGET._zeiten = []
    M.SAMMELMELDUNG_BUDGET._zeiten = []
    M.RETRY_BUDGET._zeiten = []
    M.QUELLEN_BUDGET.clear()


# --- Fall 1: technischer Fehler erzeugt niemals bestanden = 0 ---------------

@pytest.mark.parametrize("fehler", [
    LLMAuthError("Schluessel ungueltig"),
    LLMTemporaryError("Zeitueberschreitung"),
    LLMRateLimitError("429", retry_after=30),
    LLMProtocolError("kaputtes JSON"),
    RuntimeError("voellig unerwartet"),
])
def test_technischer_fehler_erzeugt_nie_bestanden_null(fehler):
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listing = _listing()

        with benutze_provider(FakeProvider([fehler])), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper([listing])], store, FakeNotifier(), _mandate())

        eintraege = store._read("SELECT * FROM match_log")
        assert eintraege == [], (
            "Ein technischer Fehler darf ueberhaupt keinen match_log-Eintrag "
            "erzeugen — weder bestanden=1 noch bestanden=0"
        )
        assert store._read("SELECT * FROM match_log WHERE bestanden = 0") == []
        store.close()


def test_unerwartete_exception_wird_nicht_zu_ablehnung():
    """Auch ein Programmierfehler darf kein fachliches Urteil werden."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listing = _listing()

        with benutze_provider(FakeProvider([AttributeError("Tippfehler im Code")])), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper([listing])], store, FakeNotifier(), _mandate())

        assert store._read("SELECT * FROM match_log") == []
        eintrag = store.get_eval_queue_entry(listing.id, listing.portal)
        assert eintrag is not None, "muss trotzdem in der Warteschlange landen"
        store.close()


def test_abgeschnittene_antwort_ist_kein_urteil():
    """max_tokens liefert halbes JSON — frueher wurde daraus 'passt nicht'."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listing = _listing()

        with benutze_provider(FakeProvider([abgeschnitten()])), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper([listing])], store, FakeNotifier(), _mandate())

        assert store._read("SELECT * FROM match_log") == []
        eintrag = store.get_eval_queue_entry(listing.id, listing.portal)
        assert eintrag["status"] == EVAL_PENDING, "ein Protokollfehler darf genau einmal wiederholt werden"
        assert eintrag["retry_count"] == 1
        store.close()


def test_evaluate_listing_wirft_statt_abzulehnen():
    """Direkt auf der Funktion: Fehler kommen als Ausnahme heraus."""
    with benutze_provider(FakeProvider([LLMTemporaryError("weg")])):
        with pytest.raises(LLMError):
            evaluate_listing(_listing(), _mandate())


def test_evaluation_hat_keinen_fallback_mehr():
    """Der Konstruktor, ueber den frueher jeder Fehler zu passt=False wurde."""
    assert not hasattr(Evaluation, "fallback"), (
        "Evaluation.fallback war der Weg, auf dem technische Fehler zu "
        "fachlichen Ablehnungen wurden — er darf nicht zurueckkehren"
    )


# --- Fall 2: Inserat bleibt nach technischem Fehler erneut bewertbar --------

def test_inserat_bleibt_nach_fehler_bewertbar():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listing = _listing()

        with benutze_provider(FakeProvider([LLMTemporaryError("Netz weg")])), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper([listing])], store, FakeNotifier(), _mandate())

        # Das Inserat gilt als gesehen — der Scraper wuerde es nie wieder anfassen.
        assert store.is_known(listing.id, listing.portal)
        # Genau deshalb muss die Warteschlange es zurueckbringen.
        faellig = store.get_due_evaluations(jetzt="2999-01-01T00:00:00")
        assert [r["listing_id"] for r in faellig] == [listing.id]
        # Und die Nutzdaten fuer eine erneute Bewertung sind vollstaendig dabei.
        assert faellig[0]["titel"] == listing.titel
        assert faellig[0]["mandate_id"] == 7
        store.close()


def test_erfolgreiche_nachbewertung_raeumt_warteschlange():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        _budgets_frei()
        listing = _listing()

        with benutze_provider(FakeProvider([LLMTemporaryError("Netz weg")])), \
             patch.object(M, "enrich_listing", lambda l: False):
            M.run_scraping_cycle([FakeScraper([listing])], store, FakeNotifier(), _mandate())
        assert store.eval_queue_groesse() == 1

        # Zweiter Anlauf, diesmal antwortet das Modell fachlich.
        ev = evaluate_listing_mit(store, listing)
        assert ev.passt is True
        assert store.eval_queue_groesse() == 0, "erledigte Arbeit muss verschwinden"
        store.close()


def evaluate_listing_mit(store, listing):
    """Hilfsfunktion: erfolgreiche Nachbewertung inkl. Persistenz."""
    with benutze_provider(FakeProvider([bewertung(passt=True, score=88)])):
        ev = evaluate_listing(listing, _mandate())
    store.save_evaluation(listing.id, listing.portal, 7, ev.__dict__)
    store.resolve_eval(listing.id, listing.portal)
    return ev


# --- Fall 10: parse_mandate speichert keinen leeren Auftrag -----------------

def test_parse_mandate_wirft_statt_leeren_auftrag_zu_liefern():
    with benutze_provider(FakeProvider([LLMTemporaryError("Zeitueberschreitung")])):
        with pytest.raises(LLMError):
            parse_mandate("3 Zimmer in Charlottenburg, max 1600 warm")


def test_parse_mandate_abgeschnitten_ist_protokollfehler():
    with benutze_provider(FakeProvider([abgeschnitten('{"zielorte": ["Charl')])):
        with pytest.raises(LLMProtocolError):
            parse_mandate("3 Zimmer in Charlottenburg")


def test_kein_leerer_auftrag_wird_gespeichert():
    """Der fruehere Rueckfall {"sonstiges": raw_text} haette einen Auftrag ohne
    jede Grenze gespeichert und dem Nutzer als Erfolg bestaetigt."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        rohtext = "3 Zimmer in Charlottenburg, max 1600 warm"

        gespeichert = True
        with benutze_provider(FakeProvider([LLMAuthError("Schluessel weg")])):
            try:
                strukturiert = parse_mandate(rohtext)
                store.save_mandate("chat1", rohtext, strukturiert)
            except LLMError:
                gespeichert = False

        assert gespeichert is False
        assert store.get_active_mandate("chat1") is None, "kein halber Auftrag in der DB"
        store.close()


def test_parse_mandate_erfolg_liefert_struktur():
    """Gegenprobe: Im Normalfall arbeitet die Funktion unveraendert."""
    with benutze_provider(FakeProvider([{"zielorte": ["Charlottenburg"], "warmmiete_max": 1600}])):
        d = parse_mandate("3 Zimmer Charlottenburg max 1600 warm")
    assert d["zielorte"] == ["Charlottenburg"]
    assert d["warmmiete_max"] == 1600
