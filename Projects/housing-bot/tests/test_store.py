"""Tests für den erweiterten Store (Audit-Trail)."""
import tempfile
from pathlib import Path
from datetime import date

from src.config import load_criteria
from src.matching import match
from src.models import Listing
from src.store import Store


def _listing(id="t1", stadtteil="Charlottenburg", warmmiete=1100.0) -> Listing:
    return Listing(
        id=id, portal="mock", url="https://x.de",
        titel="Test Wohnung", stadt="Berlin",
        stadtteil=stadtteil, warmmiete=warmmiete, flaeche=60.0, zimmer=2.0,
    )


def test_is_not_known_initially():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "test.db")
        assert not store.is_known("x", "mock")
        store.close()


def test_save_listing_marks_known():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "test.db")
        l = _listing()
        store.save_listing(l)
        assert store.is_known(l.id, l.portal)
        store.close()


def test_save_match_audit_trail():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "test.db")
        l = _listing()
        criteria = load_criteria()
        store.save_listing(l)
        result = match(l, criteria)
        store.save_match(result, geo_ok=True)
        matches = store.recent_matches(10)
        rejects = store.recent_rejects(10)
        # Je nach Kriterien ist es ein Match oder Reject
        assert len(matches) + len(rejects) >= 1
        store.close()


def test_no_duplicate_on_second_save():
    with tempfile.TemporaryDirectory() as d:
        store = Store(Path(d) / "test.db")
        l = _listing()
        store.save_listing(l)
        store.save_listing(l)  # zweites Mal: INSERT OR IGNORE
        assert store.is_known(l.id, l.portal)
        store.close()
