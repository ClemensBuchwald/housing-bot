"""Tests für IS24-Detailabruf (ID-Erkennung + Parsing) — ohne Netzwerk."""
from src.scrapers.is24_detail import (
    extract_is24_id,
    parse_features,
    parse_contact,
    parse_description,
)


def test_extract_id_aus_url():
    assert extract_is24_id("https://www.immobilienscout24.de/expose/170346126") == "170346126"


def test_extract_id_mit_query():
    url = "https://www.immobilienscout24.de/expose/170346126?utm_medium=social&referrer=taf"
    assert extract_is24_id(url) == "170346126"


def test_extract_id_aus_listing_id():
    assert extract_is24_id("is24-170346126") == "170346126"


def test_extract_id_blank():
    assert extract_is24_id("170346126") == "170346126"
    assert extract_is24_id("") is None
    assert extract_is24_id("kein-treffer") is None


def test_parse_features_check_und_text():
    data = {"sections": [{
        "type": "ATTRIBUTE_LIST",
        "attributes": [
            {"type": "TEXT", "label": "Etage:", "text": "4 von 6"},
            {"type": "CHECK", "label": "Balkon/Terrasse:"},
            {"type": "LINK", "label": "Internet:"},
        ],
    }]}
    feats = parse_features(data)
    assert "Etage:4 von 6" in feats
    assert "Balkon/Terrasse" in feats   # CHECK = vorhanden
    assert not any(f.startswith("Internet") for f in feats)  # LINK ignorieren


def test_parse_contact():
    data = {"contact": {
        "contactData": {"agent": {"name": "Frau N. Haase", "company": "COTRAC GmbH"}},
        "phoneNumbers": [],
        "mailButtonState": "active",
        "callButtonState": "inactive",
    }}
    c = parse_contact(data)
    assert c["ansprechpartner"] == "Frau N. Haase"
    assert c["firma"] == "COTRAC GmbH"
    assert c["telefon"] is None
    assert c["kontaktformular"] == "ja"
    assert c["anruf_moeglich"] == "nein"


def test_parse_description():
    data = {"sections": [
        {"type": "TEXT_AREA", "title": "Objektbeschreibung", "text": "Schöne Wohnung im 4. OG."},
        {"type": "MEDIA"},
    ]}
    assert "4. OG" in parse_description(data)
