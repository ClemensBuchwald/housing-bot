"""Tests für Schnellspur-Taktung und zeitbasierte Budgets."""
import threading
import time
from unittest.mock import patch

import src.main as M


def test_stundenbudget_greift_und_laeuft_ab():
    b = M._StundenBudget(2, "test")
    assert b.frei() is True
    b.verbrauchen(); b.verbrauchen()
    assert b.frei() is False, "Limit muss greifen"
    b._zeiten = [time.time() - 4000] * 2      # älter als eine Stunde
    assert b.frei() is True, "Alte Einträge müssen aus dem Fenster fallen"
    assert b.verbraucht() == 0


def test_schnellspur_nur_schnelle_quellen():
    """Zwischen den vollen Läufen dürfen nur die schnellen Quellen laufen."""
    laeufe = []
    stop = threading.Event()

    def fake_cycle(scrapers, store, notifier, mandate, health=None):
        laeufe.append([s.name for s in scrapers])
        if len(laeufe) >= 11:
            stop.set()
        return 0

    class FakeStore:
        def get_any_active_mandate(self):
            return {"id": 1, "raw_text": "x", "structured": {}}

    scrapers = M.build_scrapers(False)
    with patch.object(M, "run_scraping_cycle", fake_cycle), \
         patch.object(M.time, "sleep", lambda s: None):
        M.scraping_thread(scrapers, FakeStore(), None, stop, 600, 120)

    # 600/120 = 5 -> volle Läufe bei Runde 0, 5, 10
    voll = [i for i, l in enumerate(laeufe) if len(l) == len(scrapers)]
    assert voll[:3] == [0, 5, 10], f"Volle Läufe an falscher Stelle: {voll}"
    assert set(laeufe[1]) == M.SCHNELLSPUR, "Schnellspur enthält falsche Quellen"


def test_schnellspur_aus_wenn_intervall_gleich():
    """Ohne Schnellspur (fast == poll) muss jeder Lauf ein voller sein."""
    laeufe = []
    stop = threading.Event()

    def fake_cycle(scrapers, store, notifier, mandate, health=None):
        laeufe.append(len(scrapers))
        if len(laeufe) >= 3:
            stop.set()
        return 0

    class FakeStore:
        def get_any_active_mandate(self):
            return {"id": 1, "raw_text": "x", "structured": {}}

    scrapers = M.build_scrapers(False)
    with patch.object(M, "run_scraping_cycle", fake_cycle), \
         patch.object(M.time, "sleep", lambda s: None):
        M.scraping_thread(scrapers, FakeStore(), None, stop, 600, 600)

    assert all(n == len(scrapers) for n in laeufe), "Ohne Schnellspur immer volle Läufe"
