"""Regressionstest: zurückgestellte Treffer dürfen NICHT verloren gehen."""
import json
import tempfile
from pathlib import Path

import src.main as M
from src.models import Listing
from src.store import Store


class FakeNotifier:
    def __init__(self, faila=0):
        self.gesendet = []
        self.texte = []
        self.faila = faila          # erste N Sendeversuche schlagen fehl

    def send_evaluation(self, listing, evaluation):
        if self.faila > 0:
            self.faila -= 1
            return False
        self.gesendet.append(listing.id)
        return True

    def send_text(self, text, chat_id=None):
        self.texte.append(text)
        return True


def _store(tmp):
    return Store(Path(tmp) / "t.db")


def _match_ablegen(store, lid, score=80):
    l = Listing(id=lid, portal="is24", url="u", titel="Treffer", stadt="Berlin",
                stadtteil="Charlottenburg", zimmer=3.0, flaeche=95.0, warmmiete=1800.0)
    store.claim(l)
    store.save_evaluation(l.id, l.portal, 1, {
        "passt": True, "score": score, "kurzfazit": "passt",
        "vorteile": ["Balkon"], "nachteile": [], "empfehlung": "sofort anschauen",
    })
    return l


def test_zurueckgestellter_treffer_wird_nachgeholt():
    """Kernregression: Alert-Budget voll -> Treffer bleibt offen -> kommt nach."""
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        _match_ablegen(store, "is24-999")
        assert len(store.get_pending_matches()) == 1

        notifier = FakeNotifier()
        M.ALERT_BUDGET._zeiten.clear()
        n = M._nachholen(store, notifier)

        assert n == 1, "Treffer muss nachgeholt werden"
        assert notifier.gesendet == ["is24-999"]
        assert store.get_pending_matches() == [], "danach nichts mehr offen"
        store.close()


def test_fehlgeschlagener_versand_bleibt_offen():
    """Schlaegt Telegram fehl, darf der Treffer nicht als erledigt gelten."""
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        _match_ablegen(store, "is24-888")
        notifier = FakeNotifier(faila=1)     # erster Versuch scheitert
        M.ALERT_BUDGET._zeiten.clear()

        assert M._nachholen(store, notifier) == 0
        assert len(store.get_pending_matches()) == 1, "muss offen bleiben"

        # Zweiter Anlauf klappt
        assert M._nachholen(store, notifier) == 1
        assert store.get_pending_matches() == []
        store.close()


def test_nachholen_kostet_keine_ki_bewertung():
    """Die Bewertung liegt gespeichert vor — Nachholen darf nichts kosten."""
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        _match_ablegen(store, "is24-777")
        row = store.get_pending_matches()[0]
        ev = json.loads(row["evaluation"])
        assert ev["vorteile"] == ["Balkon"], "Vor-/Nachteile muessen erhalten sein"
        assert ev["empfehlung"] == "sofort anschauen"
        store.close()


def test_claim_verhindert_doppelte_verarbeitung():
    """Zwei Threads duerfen dasselbe Inserat nicht beide als neu sehen."""
    with tempfile.TemporaryDirectory() as d:
        store = _store(d)
        l = Listing(id="is24-1", portal="is24", url="u", titel="T", stadt="Berlin")
        assert store.claim(l) is True
        assert store.claim(l) is False
        store.close()


def test_sammelmeldung_ist_gedrosselt():
    """Nicht jeder 2-Minuten-Zyklus darf eine Sammelmeldung schicken."""
    notifier = FakeNotifier()
    M.SAMMELMELDUNG_BUDGET._zeiten.clear()
    for _ in range(6):
        M._sammelmeldung(notifier, 3)
    assert len(notifier.texte) <= 2, f"zu viele Sammelmeldungen: {len(notifier.texte)}"
