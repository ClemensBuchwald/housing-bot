from datetime import date

from src.config import load_criteria
from src.matching import match
from src.models import Listing


def _listing(**kwargs) -> Listing:
    defaults = dict(
        id="test-1",
        portal="mock",
        url="https://example.com/1",
        titel="Test Wohnung",
        stadt="Berlin",
        stadtteil="Prenzlauer Berg",
        kaltmiete=900.0,
        warmmiete=1100.0,
        flaeche=60.0,
        zimmer=2.0,
        verfuegbar_ab=date(2026, 7, 1),
        merkmale=["Balkon", "Haustiere erlaubt"],
    )
    defaults.update(kwargs)
    return Listing(**defaults)


def test_gutes_inserat_besteht():
    criteria = load_criteria()
    result = match(_listing(), criteria)
    assert result.bestanden is True
    assert result.score > 0


def test_zu_teuer_abgelehnt():
    criteria = load_criteria()
    result = match(_listing(warmmiete=1800.0), criteria)
    assert result.bestanden is False
    assert "Preis" in result.ablehnungsgrund


def test_zu_klein_abgelehnt():
    criteria = load_criteria()
    result = match(_listing(flaeche=30.0), criteria)
    assert result.bestanden is False
    assert "Fläche" in result.ablehnungsgrund


def test_ausschlusswort_abgelehnt():
    criteria = load_criteria()
    result = match(_listing(titel="Zwischenmiete 2 Zimmer"), criteria)
    assert result.bestanden is False
    assert "Zwischenmiete" in result.ablehnungsgrund


def test_preis_unbekannt_nicht_gefiltert():
    criteria = load_criteria()
    result = match(_listing(warmmiete=None, kaltmiete=None), criteria)
    assert result.bestanden is True


def test_score_bevorzugter_stadtteil():
    criteria = load_criteria()
    result = match(_listing(stadtteil="Prenzlauer Berg"), criteria)
    assert result.score >= 20


def test_score_nicht_bevorzugter_stadtteil():
    criteria = load_criteria()
    r_bevorzugt = match(_listing(stadtteil="Prenzlauer Berg"), criteria)
    r_anderer = match(_listing(stadtteil="Spandau"), criteria)
    assert r_bevorzugt.score > r_anderer.score


def test_halbe_zimmer_aufgerundet():
    l = _listing(zimmer=2.5)
    assert l.zimmer_gerundet == 3


def test_geo_filter_nutzt_plz_aus_merkmalen():
    """PLZ steckt bei mehreren Quellen nur in den Merkmalen — geo.py muss sie lesen."""
    from src.scrapers.geo import in_zielgebiet
    l = Listing(id="x", portal="wbm", url="https://wbm.de/x",
                titel="2-Zimmer-Wohnung", stadt="Berlin",
                merkmale=["PLZ:10623", "Adresse:Teststr. 1, 10623 Berlin"])
    assert in_zielgebiet(l) is True


def test_geo_filter_lehnt_fremde_plz_ab():
    from src.scrapers.geo import in_zielgebiet
    l = Listing(id="y", portal="wbm", url="https://wbm.de/y",
                titel="2-Zimmer-Wohnung", stadt="Berlin",
                merkmale=["PLZ:13055", "Adresse:Testweg 9, 13055 Berlin"])
    assert in_zielgebiet(l) is False


def test_geo_lehnt_nachbar_ortsteil_ab():
    """Westend liegt im selben Bezirk und teilt PLZ — ist aber kein Zielort."""
    from src.scrapers.geo import in_zielgebiet
    l = Listing(id="w", portal="immowelt", url="https://immowelt.de/expose/x",
                titel="3 Zi", stadt="Berlin", stadtteil="Westend",
                merkmale=["PLZ:14052"])
    assert in_zielgebiet(l) is False


def test_geo_bezirksname_wird_nicht_als_ortsteil_gelesen():
    from src.scrapers.geo import extract_stadtteil
    # Bezirk "Charlottenburg-Wilmersdorf" darf nicht "Wilmersdorf" ergeben
    assert extract_stadtteil("Charlottenburg, Charlottenburg-Wilmersdorf (10587)") == "Charlottenburg"


def test_geo_plz_keine_zufallstreffer_aus_url():
    """Ziffernfolgen in Anzeigen-IDs dürfen keine PLZ-Treffer erzeugen."""
    from src.scrapers.geo import in_zielgebiet
    l = Listing(id="k", portal="kleinanzeigen",
                url="https://www.kleinanzeigen.de/s-anzeige/x/10719123-203-3365",
                titel="Wohnung in Neukoelln", stadt="Berlin", merkmale=["PLZ:12043"])
    assert in_zielgebiet(l) is False
