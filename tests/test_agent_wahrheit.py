"""Der Agent darf nie etwas anderes behaupten, als tatsächlich geschehen ist.

Zwei Fälle waren gefährlich:

1. ``stop_reason = max_tokens``: Das Modell beginnt "Klar, ich speichere dir das
   ab …", läuft ins Token-Limit und ruft nie ein Werkzeug auf. Der angefangene
   Satz ging trotzdem als Antwort raus — der Nutzer glaubte an einen laufenden
   Suchauftrag, den es nie gab.

2. Fehler NACH einem Werkzeugaufruf: Der Auftrag war gespeichert, dann brach die
   Verbindung ab — und der Nutzer bekam "Ups, da ist etwas schiefgelaufen".
"""
import tempfile
from pathlib import Path

from src.agent import ConversationAgent
from src.llm.errors import LLMAuthError, LLMRateLimitError, LLMTemporaryError
from src.store import Store
from tests.fakes import FakeProvider, antwort, benutze_provider, werkzeugaufruf
from tests.fakes import abgeschnitten


def _agent(store, **kw):
    return ConversationAgent(store, sources_text="- Testquelle", **kw)


_AUFTRAG_ARGS = {
    "zusammenfassung": "3 Zimmer in Charlottenburg, max 1600 warm",
    "zielorte": ["Charlottenburg"],
    "warmmiete_max": 1600,
    "zimmer_min": 3,
}


# --- Fall 11: max_tokens behauptet keine nicht ausgefuehrte Aktion ---------

def test_max_tokens_ohne_werkzeug_behauptet_nichts():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)

        angefangen = "Klar! Ich speichere dir den Suchauftrag jetzt ab und starte die Suche für"
        with benutze_provider(FakeProvider([abgeschnitten(angefangen)])):
            reply = agent.handle("chat1", "3 Zimmer in Charlottenburg bitte")

        assert store.get_active_mandate("chat1") is None, "es wurde nichts gespeichert"
        assert angefangen not in reply, (
            "der abgeschnittene Satz behauptet eine Speicherung, die nie stattfand"
        )
        assert "abgeschnitten" in reply.lower()
        assert "nichts gespeichert" in reply.lower() or "nichts geändert" in reply.lower()
        store.close()


def test_max_tokens_nach_werkzeug_nennt_die_echte_aktion():
    """Hier IST der Auftrag gespeichert — das muss auch so gesagt werden."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)

        with benutze_provider(FakeProvider([
            werkzeugaufruf("suchauftrag_speichern", _AUFTRAG_ARGS),
            abgeschnitten("Alles klar, ich habe deinen Auftrag gespeichert und suche ab"),
        ])):
            reply = agent.handle("chat1", "3 Zimmer in Charlottenburg bitte")

        m = store.get_active_mandate("chat1")
        assert m is not None, "der Werkzeugaufruf lief tatsaechlich"
        assert "gespeichert" in reply.lower()
        assert "suche" in reply.lower()
        store.close()


def test_leere_antwort_wird_nicht_zu_ok():
    """Frueher wurde jede inhaltslose Antwort zu einem munteren 'Ok!'."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)
        with benutze_provider(FakeProvider([antwort("")])):
            reply = agent.handle("chat1", "hallo")
        assert reply != "Ok!"
        assert store.get_active_mandate("chat1") is None
        store.close()


# --- Fall 12: Fehler nach ausgefuehrtem Werkzeug ---------------------------

def test_fehler_nach_gespeichertem_auftrag_meldet_wahrheit():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)

        with benutze_provider(FakeProvider([
            werkzeugaufruf("suchauftrag_speichern", _AUFTRAG_ARGS),
            LLMTemporaryError("Verbindung weg"),
        ])):
            reply = agent.handle("chat1", "3 Zimmer in Charlottenburg bitte")

        m = store.get_active_mandate("chat1")
        assert m is not None, "der Auftrag IST gespeichert"
        assert m["structured"]["warmmiete_max"] == 1600

        assert "gespeichert" in reply.lower(), (
            "der Nutzer muss erfahren, dass sein Auftrag laeuft — auch wenn die "
            "Erklaerung des Modells verloren ging"
        )
        assert "schiefgelaufen" not in reply.lower(), \
            "pauschales Scheitern waere hier gelogen"
        store.close()


def test_fehler_ohne_werkzeug_bleibt_normale_entschuldigung():
    """Gegenprobe: Ohne vollzogene Aktion ist die allgemeine Fehlermeldung richtig."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)
        with benutze_provider(FakeProvider([LLMAuthError("Schluessel weg")])):
            reply = agent.handle("chat1", "hallo")
        assert "schiefgelaufen" in reply.lower()
        assert "gespeichert" not in reply.lower()
        store.close()


def test_fehler_nach_pausieren_nennt_pausiert():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        store.save_mandate("chat1", "alter Auftrag", {})
        agent = _agent(store)

        with benutze_provider(FakeProvider([
            werkzeugaufruf("suche_pausieren"),
            LLMRateLimitError("429"),
        ])):
            reply = agent.handle("chat1", "pausier mal bitte")

        assert store.get_active_mandate("chat1") is None
        assert store.get_paused_mandate("chat1") is not None
        assert "pausiert" in reply.lower()
        store.close()


def test_gescheitertes_pausieren_wird_nicht_als_erfolg_gemeldet():
    """Ohne aktiven Auftrag aendert das Werkzeug nichts — dann darf der
    Fehlertext auch keine Pausierung behaupten."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)

        with benutze_provider(FakeProvider([
            werkzeugaufruf("suche_pausieren"),
            LLMTemporaryError("weg"),
        ])):
            reply = agent.handle("chat1", "pausier mal")

        assert "pausiert" not in reply.lower()
        assert "schiefgelaufen" in reply.lower()
        store.close()


def test_bereits_gesendete_treffer_werden_nicht_verleugnet():
    """Die Sofort-Suche schickt die Treffer direkt in den Chat. Faellt das
    Modell danach aus, sind sie beim Nutzer — die Antwort darf das nicht
    als Fehlschlag darstellen."""
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        gesendet = []

        def such_fn(criteria, mandate, include_seen=False):
            return [{"titel": "Treffer 1", "quelle": "is24", "url": "https://x.invalid/1",
                     "score": 90, "ort": "Charlottenburg"}]

        agent = _agent(store, search_fn=such_fn,
                       notify_fn=lambda cid, text: gesendet.append(text))

        with benutze_provider(FakeProvider([
            werkzeugaufruf("jetzt_angebote_suchen", {}),
            LLMTemporaryError("weg"),
        ])):
            reply = agent.handle("chat1", "zeig mal was es gerade gibt")

        assert len(gesendet) == 1, "der Treffer ging tatsaechlich raus"
        assert "schiefgelaufen" not in reply.lower()
        assert "treffer" in reply.lower() or "oben" in reply.lower()
        store.close()


def test_erfolglose_sofortsuche_behauptet_keine_treffer():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store, search_fn=lambda c, m, include_seen=False: [],
                       notify_fn=lambda cid, text: None)

        with benutze_provider(FakeProvider([
            werkzeugaufruf("jetzt_angebote_suchen", {}),
            LLMTemporaryError("weg"),
        ])):
            reply = agent.handle("chat1", "zeig mal")

        assert "oben im chat" not in reply.lower(), "es wurde nichts geschickt"
        store.close()


# --- Normalbetrieb bleibt unveraendert -------------------------------------

def test_normale_unterhaltung_unveraendert():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)
        with benutze_provider(FakeProvider([antwort("Hi! Wonach sucht ihr denn?")])):
            reply = agent.handle("chat1", "hallo")
        assert reply == "Hi! Wonach sucht ihr denn?"
        assert store.get_chat_history("chat1") == [
            {"role": "user", "content": "hallo"},
            {"role": "assistant", "content": "Hi! Wonach sucht ihr denn?"},
        ]
        store.close()


def test_werkzeug_und_danach_normale_antwort():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        agent = _agent(store)
        with benutze_provider(FakeProvider([
            werkzeugaufruf("suchauftrag_speichern", _AUFTRAG_ARGS),
            antwort("Alles klar, ich suche ab jetzt für euch!"),
        ])):
            reply = agent.handle("chat1", "3 Zimmer Charlottenburg")
        assert reply == "Alles klar, ich suche ab jetzt für euch!"
        assert store.get_active_mandate("chat1") is not None
        store.close()


def test_agent_nutzt_keine_direkte_verbindung_mehr():
    """Der fruehere Direktzugriff auf store._conn lief am Lock vorbei."""
    quelltext = Path("src/agent.py").read_text()
    assert "store._conn" not in quelltext
    assert "anthropic" not in quelltext
