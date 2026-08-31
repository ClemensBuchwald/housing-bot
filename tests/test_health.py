"""Zustandsüberwachung, /zustand und Fehlerbehandlung der Threads.

Die alte Docker-Prüfung legte nur ``/app/data`` an. Sie war selbst dann noch
grün, wenn der Scraping-Thread längst gestorben war — der Bot galt als gesund
und suchte doch seit Stunden nicht mehr.
"""
import json
import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import src.main as M
from src.health import (
    DEGRADED,
    HEALTHY,
    UNHEALTHY,
    Health,
    bewerten,
    lesen,
)
from src.healthcheck import main as healthcheck_main
from src.store import Store
from src.telegram_handler import TelegramHandler


def _gesunder_zustand(**abweichung) -> dict:
    jetzt = time.time()
    daten = {
        "gestartet_am": "2026-08-31T20:00:00",
        "threads": {
            "scraping": {"zeit": "…", "zeitstempel": jetzt, "max_alter_s": 1800},
            "telegram": {"zeit": "…", "zeitstempel": jetzt, "max_alter_s": 180},
        },
        "zyklus": {"beendet_am": "2026-08-31T21:00:00", "quellen": 10,
                   "quellen_fehler": 0, "inserate": 143, "treffer": 2},
        "llm": {"zustand": "geschlossen"},
        "queue": {},
        "telegram": {"letzter_erfolg": "…", "letzter_fehler": None, "fehlerserie": 0},
        "db": {"ok": True, "letzter_fehler": None},
    }
    daten.update(abweichung)
    return daten


# --- Bewertung --------------------------------------------------------------

def test_healthy_wenn_alles_laeuft():
    status, gruende = bewerten(_gesunder_zustand())
    assert status == HEALTHY and gruende == []


def test_degraded_bei_llm_ausfall():
    status, gruende = bewerten(_gesunder_zustand(
        llm={"zustand": "offen", "kategorie": "auth", "gesperrt_noch_s": 0}))
    assert status == DEGRADED
    assert "auth" in gruende[0]


def test_degraded_bei_angestauter_warteschlange():
    status, gruende = bewerten(_gesunder_zustand(queue={"pending": 60, "blocked": 0}))
    assert status == DEGRADED
    assert "angestaut" in gruende[0]


def test_degraded_wenn_telegram_dauerhaft_nicht_antwortet():
    status, gruende = bewerten(_gesunder_zustand(
        telegram={"fehlerserie": 9, "letzter_fehler": {"code": 401}, "letzter_erfolg": None}))
    assert status == DEGRADED
    assert "401" in gruende[0]


def test_unhealthy_bei_totem_scraping_thread():
    daten = _gesunder_zustand()
    daten["threads"]["scraping"]["zeitstempel"] = time.time() - 4000
    status, gruende = bewerten(daten)
    assert status == UNHEALTHY
    assert "scraping" in gruende[0]


def test_unhealthy_bei_totem_telegram_thread():
    daten = _gesunder_zustand()
    daten["threads"]["telegram"]["zeitstempel"] = time.time() - 600
    status, _ = bewerten(daten)
    assert status == UNHEALTHY


def test_unhealthy_wenn_ein_thread_fehlt():
    daten = _gesunder_zustand()
    del daten["threads"]["scraping"]
    status, gruende = bewerten(daten)
    assert status == UNHEALTHY
    assert any("scraping" in g for g in gruende)


def test_unhealthy_ohne_zustandsdatei():
    status, gruende = bewerten(None)
    assert status == UNHEALTHY


def test_unhealthy_bei_defekter_datenbank():
    status, _ = bewerten(_gesunder_zustand(), db_ok=False)
    assert status == UNHEALTHY


def test_schwerer_fehler_schlaegt_leichten():
    daten = _gesunder_zustand(llm={"zustand": "offen", "kategorie": "auth"})
    daten["threads"]["scraping"]["zeitstempel"] = time.time() - 4000
    status, _ = bewerten(daten)
    assert status == UNHEALTHY


# --- Schreiben und Lesen ----------------------------------------------------

def test_zustand_wird_atomar_geschrieben():
    with tempfile.TemporaryDirectory() as d:
        pfad = Path(d) / "health.json"
        h = Health(pfad, schreib_intervall_s=0)
        h.herzschlag("scraping")
        h.zyklus_beendet(10, 1, 143, 2, 61.2)

        daten = lesen(pfad)
        assert daten["zyklus"]["quellen_fehler"] == 1
        assert daten["threads"]["scraping"]["max_alter_s"] == 1800
        assert not (Path(d) / "health.json.tmp").exists(), "keine Reste"
        json.loads(pfad.read_text())        # muss vollständiges JSON sein


def test_schreiben_wird_gedrosselt():
    with tempfile.TemporaryDirectory() as d:
        pfad = Path(d) / "health.json"
        h = Health(pfad, schreib_intervall_s=60)
        h.herzschlag("telegram")
        erste = pfad.stat().st_mtime_ns
        for _ in range(50):
            h.herzschlag("telegram")
        assert pfad.stat().st_mtime_ns == erste, "50 Herzschläge, ein Schreibvorgang"


def test_unbeschreibbarer_pfad_wirft_nicht():
    """Die Zustandsdatei darf den Bot niemals stoppen."""
    h = Health(Path("/gibt/es/nicht/health.json"), schreib_intervall_s=0)
    h.herzschlag("scraping")        # darf keine Ausnahme werfen


# --- Healthcheck-Kommando ---------------------------------------------------

def test_healthcheck_gibt_null_zurueck_wenn_gesund(tmp_path, monkeypatch):
    db = tmp_path / "housing_bot.db"
    Store(db).close()
    health_pfad = tmp_path / "health.json"
    h = Health(health_pfad, schreib_intervall_s=0)
    h.herzschlag("scraping"); h.herzschlag("telegram")

    monkeypatch.setattr("src.healthcheck.DEFAULT_DB_PATH", db)
    monkeypatch.setattr("src.healthcheck.lesen", lambda pfad=None: lesen(health_pfad))
    assert healthcheck_main([]) == 0


def test_healthcheck_bleibt_null_bei_degraded(tmp_path, monkeypatch, capsys):
    db = tmp_path / "housing_bot.db"
    Store(db).close()
    daten = _gesunder_zustand(llm={"zustand": "offen", "kategorie": "auth"})
    monkeypatch.setattr("src.healthcheck.DEFAULT_DB_PATH", db)
    monkeypatch.setattr("src.healthcheck.lesen", lambda pfad=None: daten)

    assert healthcheck_main([]) == 0, (
        "degraded darf keinen Neustart ausloesen — der wuerde die Stoerung nicht "
        "beheben, aber den Verlauf abreissen"
    )
    assert "DEGRADED" in capsys.readouterr().out


def test_healthcheck_gibt_eins_zurueck_bei_totem_thread(tmp_path, monkeypatch, capsys):
    db = tmp_path / "housing_bot.db"
    Store(db).close()
    daten = _gesunder_zustand()
    daten["threads"]["scraping"]["zeitstempel"] = time.time() - 5000
    monkeypatch.setattr("src.healthcheck.DEFAULT_DB_PATH", db)
    monkeypatch.setattr("src.healthcheck.lesen", lambda pfad=None: daten)

    assert healthcheck_main([]) == 1
    assert "UNHEALTHY" in capsys.readouterr().out


def test_healthcheck_meldet_fehlende_datenbank(tmp_path, monkeypatch):
    monkeypatch.setattr("src.healthcheck.DEFAULT_DB_PATH", tmp_path / "gibtsnicht.db")
    monkeypatch.setattr("src.healthcheck.lesen", lambda pfad=None: _gesunder_zustand())
    assert healthcheck_main([]) == 1


def test_healthcheck_ruft_kein_modell_auf(tmp_path, monkeypatch):
    """Ein Gesundheitscheck, der das Modell anruft, wäre bei einem
    Anbieterausfall genau dann stumm, wenn man ihn braucht."""
    from src.llm import factory
    aufrufe = []

    class Wachhund:
        name = model = "wachhund"

        def complete(self, **kw):
            aufrufe.append(kw)
            raise AssertionError("der Healthcheck darf kein Modell anrufen")

    factory.set_provider(Wachhund())
    try:
        db = tmp_path / "housing_bot.db"
        Store(db).close()
        monkeypatch.setattr("src.healthcheck.DEFAULT_DB_PATH", db)
        monkeypatch.setattr("src.healthcheck.lesen", lambda pfad=None: _gesunder_zustand())
        healthcheck_main([])
        assert aufrufe == []
    finally:
        factory.set_provider(None)


# --- /zustand ---------------------------------------------------------------

def test_zustand_funktioniert_ohne_modell():
    from src.llm import factory
    factory.set_provider(None)
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        health.herzschlag("scraping"); health.herzschlag("telegram")
        store.save_mandate("c1", "3 Zimmer Charlottenburg", {"zimmer_min": 3})
        health.zyklus_beendet(10, 0, 143, 2, 61.0)

        h = TelegramHandler(store, health=health)
        h.chat_id = "c1"
        bericht = h.zustandsbericht()

        assert "Bot läuft" in bericht
        assert "Suche aktiv" in bericht
        assert "143 Inserate" in bericht
        assert "Wiedervorlage" in bericht
        assert "Bewertung" in bericht
        store.close()


def test_zustand_zeigt_stoerung_an():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        health.herzschlag("scraping"); health.herzschlag("telegram")
        health.llm_zustand({"zustand": "offen", "kategorie": "auth", "gesperrt_noch_s": 0})
        store.enqueue_eval("a", "is24", 1, "401", verbraucht_versuch=False,
                           max_versuche=0, retryable=False)
        health.queue_zustand(store.eval_queue_zaehler())

        bericht = TelegramHandler(store, health=health).zustandsbericht()
        assert "gestört" in bericht and "auth" in bericht
        assert "1 blockiert" in bericht
        store.close()


def test_zustand_enthaelt_keine_geheimnisse(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1234:GEHEIMER-TOKEN")
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        health.herzschlag("scraping")
        health.telegram_fehler(401, "Token ungültig oder widerrufen")
        bericht = TelegramHandler(store, health=health).zustandsbericht()
        assert "GEHEIMER-TOKEN" not in bericht
        assert "Traceback" not in bericht
        store.close()


def test_zustand_ist_als_befehl_erreichbar():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        health.herzschlag("scraping")
        h = TelegramHandler(store, health=health)
        gesendet = []
        h.send = lambda cid, text: gesendet.append(text)
        h._handle_command("c1", "/zustand")
        assert gesendet and "Bot läuft" in gesendet[0]
        store.close()


# --- Telegram-Fehler --------------------------------------------------------

class _Antwort:
    def __init__(self, code):
        self.status_code = code
        self.is_success = 200 <= code < 300

    def json(self):
        return {"result": []}


@pytest.mark.parametrize("code,stichwort", [
    (401, "Token ungültig"),
    (403, "blockiert"),
    (404, "Endpunkt"),
    (409, "zweiter Abrufer"),
    (429, "drosselt"),
])
def test_telegram_fehlercodes_werden_erkannt(code, stichwort, monkeypatch, caplog):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        h = TelegramHandler(store, health=health)
        monkeypatch.setattr("src.telegram_handler.httpx.get", lambda *a, **k: _Antwort(code))

        with caplog.at_level("WARNING"):
            h.poll_once()

        daten = health.schnappschuss()["telegram"]
        assert daten["fehlerserie"] == 1
        assert daten["letzter_fehler"]["code"] == code
        assert stichwort in daten["letzter_fehler"]["hinweis"]
        assert any(str(code) in r.getMessage() for r in caplog.records), \
            "der Fehler muss im Log stehen"
        store.close()


def test_telegram_fehler_wird_nicht_mehr_verschluckt(monkeypatch):
    """Vorher: 'if not resp.is_success: return' — ohne jede Spur."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        h = TelegramHandler(store, health=health)
        monkeypatch.setattr("src.telegram_handler.httpx.get", lambda *a, **k: _Antwort(401))

        for _ in range(6):
            h.poll_once()

        status, gruende = bewerten(_gesunder_zustand(
            telegram=health.schnappschuss()["telegram"]))
        assert status == DEGRADED, "eine Dauerstörung muss sichtbar werden"
        store.close()


def test_erfolgreicher_abruf_setzt_die_fehlerserie_zurueck(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test:token")
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        h = TelegramHandler(store, health=health)

        monkeypatch.setattr("src.telegram_handler.httpx.get", lambda *a, **k: _Antwort(429))
        h.poll_once(); h.poll_once()
        assert health.schnappschuss()["telegram"]["fehlerserie"] == 2

        monkeypatch.setattr("src.telegram_handler.httpx.get", lambda *a, **k: _Antwort(200))
        h.poll_once()
        assert health.schnappschuss()["telegram"]["fehlerserie"] == 0
        store.close()


# --- Scraping-Thread überlebt Datenbankfehler -------------------------------

def test_datenbankfehler_beendet_den_scraping_thread_nicht():
    """Vorher lag get_any_active_mandate() ausserhalb des try — ein einzelner
    Fehler beendete den Thread lautlos, der Bot suchte nie wieder."""
    with tempfile.TemporaryDirectory() as d:
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        stop = threading.Event()
        runden = {"n": 0}

        class KaputterStore:
            def get_any_active_mandate(self):
                runden["n"] += 1
                if runden["n"] <= 3:
                    raise sqlite3.OperationalError("database is locked")
                if runden["n"] >= 6:
                    stop.set()
                return None

        with patch.object(M.time, "sleep", lambda s: None):
            M.scraping_thread([], KaputterStore(), None, stop, 600, 120, health)

        assert runden["n"] >= 6, "der Thread muss die Fehler ueberlebt haben"
        assert health.schnappschuss()["db"]["ok"] is True, \
            "nach erfolgreichen Runden gilt die DB wieder als in Ordnung"


def test_datenbankfehler_wird_im_zustand_vermerkt():
    with tempfile.TemporaryDirectory() as d:
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        stop = threading.Event()
        runden = {"n": 0}

        class ImmerKaputt:
            def get_any_active_mandate(self):
                runden["n"] += 1
                if runden["n"] >= 3:
                    stop.set()
                raise sqlite3.OperationalError("disk I/O error")

        with patch.object(M.time, "sleep", lambda s: None):
            M.scraping_thread([], ImmerKaputt(), None, stop, 600, 120, health)

        db = health.schnappschuss()["db"]
        assert db["ok"] is False
        assert "disk I/O" in db["letzter_fehler"]
        status, _ = bewerten(health.schnappschuss())
        assert status == UNHEALTHY


def test_beliebige_ausnahme_beendet_den_thread_nicht():
    with tempfile.TemporaryDirectory() as d:
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        stop = threading.Event()
        runden = {"n": 0}

        class Launisch:
            def get_any_active_mandate(self):
                runden["n"] += 1
                if runden["n"] >= 5:
                    stop.set()
                raise ValueError("irgendwas Unerwartetes")

        with patch.object(M.time, "sleep", lambda s: None):
            M.scraping_thread([], Launisch(), None, stop, 600, 120, health)
        assert runden["n"] >= 5


def test_scraping_thread_gibt_lebenszeichen():
    with tempfile.TemporaryDirectory() as d:
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        stop = threading.Event()
        runden = {"n": 0}

        class Leer:
            def get_any_active_mandate(self):
                runden["n"] += 1
                if runden["n"] >= 2:
                    stop.set()
                return None

        with patch.object(M.time, "sleep", lambda s: None):
            M.scraping_thread([], Leer(), None, stop, 600, 120, health)

        assert "scraping" in health.schnappschuss()["threads"]


# --- Quellenausfall ist kein leeres Ergebnis --------------------------------

def test_quellenausfall_wird_als_stoerung_erkannt(caplog):
    from tests.test_silent_failure import FakeNotifier, _budgets_frei, _mandate
    from tests.fakes import FakeProvider, benutze_provider

    class KaputteQuelle:
        name = "is24"

        def fetch_listings(self, criteria):
            raise ConnectionError("Portal nicht erreichbar")

    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        _budgets_frei()

        with benutze_provider(FakeProvider([])), caplog.at_level("WARNING"):
            M.run_scraping_cycle([KaputteQuelle()], store, FakeNotifier(),
                                 _mandate(), health)

        zyklus = health.schnappschuss()["zyklus"]
        assert zyklus["quellen_fehler"] == 1
        assert zyklus["inserate"] == 0
        assert any("ausgefallen" in r.getMessage() for r in caplog.records), \
            "ein Ausfall muss anders aussehen als ein leeres Ergebnis"
        store.close()


def test_leeres_ergebnis_ist_kein_fehler():
    from tests.test_silent_failure import FakeNotifier, FakeScraper, _budgets_frei, _mandate
    from tests.fakes import FakeProvider, benutze_provider

    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "t.db")
        health = Health(Path(d) / "health.json", schreib_intervall_s=0)
        _budgets_frei()
        with benutze_provider(FakeProvider([])):
            M.run_scraping_cycle([FakeScraper([])], store, FakeNotifier(), _mandate(), health)

        assert health.schnappschuss()["zyklus"]["quellen_fehler"] == 0
        store.close()


# --- Zugangsdaten dürfen nicht in Logs landen -------------------------------

def test_telegram_token_wird_aus_logs_entfernt():
    """httpx protokolliert die volle URL — bei Telegram steht der Token im Pfad.
    Ohne Filter liegt er im Klartext in den Container-Logs."""
    import logging as _logging
    f = M._GeheimnisFilter()
    satz = ("HTTP Request: GET https://api.telegram.org/"
            "bot8897458081:AAE3QMIanA1opiHE0AXVWJoIUNAP1uk8T9E/getUpdates \"200 OK\"")
    record = _logging.LogRecord("httpx", _logging.INFO, "x", 1, satz, None, None)
    f.filter(record)
    assert "AAE3QMIanA1opiHE0AXVWJoIUNAP1uk8T9E" not in record.getMessage()
    assert "entfernt" in record.getMessage()
    assert "api.telegram.org" in record.getMessage(), "die URL bleibt nachvollziehbar"


def test_anthropic_schluessel_wird_aus_logs_entfernt():
    import logging as _logging
    f = M._GeheimnisFilter()
    record = _logging.LogRecord("x", _logging.ERROR, "x", 1,
                                "Fehler mit sk-ant-api03-AbCdEfGhIjKlMnOp", None, None)
    f.filter(record)
    assert "AbCdEfGhIjKlMnOp" not in record.getMessage()


def test_filter_laesst_normale_zeilen_unveraendert():
    import logging as _logging
    f = M._GeheimnisFilter()
    record = _logging.LogRecord("x", _logging.INFO, "x", 1,
                                "150 Inserate von %s", ("is24",), None)
    f.filter(record)
    assert record.getMessage() == "150 Inserate von is24"


def test_filter_haengt_an_der_wurzel():
    """Damit er unabhaengig von der erzeugenden Bibliothek wirkt."""
    import logging as _logging
    wurzel = _logging.getLogger()
    assert any(isinstance(fl, M._GeheimnisFilter)
               for h in wurzel.handlers for fl in h.filters), \
        "der Filter muss an den Wurzel-Handlern haengen"
