"""ImmoScout24 Detail-Abruf: Ausstattungsmerkmale + Anbieter-Kontaktdaten.

Technischer Befund (Live-Analyse 2026-08):
  GET https://api.mobile.immobilienscout24.de/expose/{id}
  Header: User-Agent: ImmoScout24_2410_28_._

Antwortstruktur:
  sections[] mit type:
    ATTRIBUTE_LIST  → attributes[] mit
                       type "TEXT"  {label, text}   z.B. "Etage:" = "4 von 6"
                       type "CHECK" {label}         = Merkmal VORHANDEN (z.B. Balkon/Terrasse)
    TEXT_AREA       → {title, text}  Objektbeschreibung / Lage / Sonstiges
  contact.contactData.agent → {name, company}
  contact.phoneNumbers[]    → Telefonnummern (oft leer)
  contact.mailButtonState   → "active" = Kontaktformular verfügbar

Damit lassen sich Kriterien prüfen, die in der Trefferliste fehlen:
Etage/Erdgeschoss, Balkon/Terrasse, Keller, Aufzug, Haustiere, Kaution.

Datenschutz: Es werden ausschließlich die im Inserat öffentlich sichtbaren
gewerblichen Anbieterdaten (Makler/Firma) ausgelesen — keine Logins, keine
Umgehung von Zugangsschranken, keine privaten Kontaktdaten Dritter.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.mobile.immobilienscout24.de/expose/{}"
_HEADERS = {"User-Agent": "ImmoScout24_2410_28_._", "Accept": "application/json"}
_TIMEOUT = 15


def extract_is24_id(url_or_id: str) -> Optional[str]:
    """Holt die Scout-ID aus URL, 'is24-<id>' oder blanker ID."""
    if not url_or_id:
        return None
    s = str(url_or_id)
    m = re.search(r"/expose/(\d{6,})", s) or re.search(r"is24-(\d{6,})", s) or re.fullmatch(r"\s*(\d{6,})\s*", s)
    return m.group(1) if m else None


def fetch_expose(listing_id: str) -> Optional[dict]:
    """Rohes Expose-JSON holen (None bei Fehler)."""
    try:
        r = httpx.get(_API.format(listing_id), headers=_HEADERS, timeout=_TIMEOUT)
        if r.status_code != 200:
            logger.debug("[is24_detail] HTTP %s für %s", r.status_code, listing_id)
            return None
        return r.json()
    except Exception as e:
        logger.debug("[is24_detail] Fehler bei %s: %s", listing_id, e)
        return None


def parse_features(data: dict) -> List[str]:
    """Ausstattungsmerkmale als lesbare Liste ('Etage:4 von 6', 'Balkon/Terrasse')."""
    out: List[str] = []
    for sec in data.get("sections", []):
        if sec.get("type") != "ATTRIBUTE_LIST":
            continue
        for a in sec.get("attributes", []):
            label = (a.get("label") or "").strip().rstrip(":")
            if not label:
                continue
            if a.get("type") == "CHECK":
                out.append(label)                      # vorhanden
            elif a.get("type") == "TEXT" and a.get("text"):
                out.append(f"{label}:{a['text']}")
    return out


def parse_description(data: dict, max_len: int = 900) -> str:
    """Objektbeschreibung + Lage + Sonstiges zusammenfassen."""
    parts = []
    for sec in data.get("sections", []):
        if sec.get("type") == "TEXT_AREA" and sec.get("text"):
            parts.append(f"{sec.get('title','')}: {sec['text']}".strip())
    return " | ".join(parts)[:max_len]


def parse_contact(data: dict) -> Dict[str, Optional[str]]:
    """Öffentlich sichtbare gewerbliche Anbieterdaten des Inserats."""
    c = data.get("contact") or {}
    agent = (c.get("contactData") or {}).get("agent") or {}
    phones = c.get("phoneNumbers") or []
    phone = None
    if phones:
        p0 = phones[0]
        phone = p0.get("number") if isinstance(p0, dict) else str(p0)
    return {
        "ansprechpartner": agent.get("name"),
        "firma": agent.get("company"),
        "telefon": phone,
        "kontaktformular": "ja" if c.get("mailButtonState") == "active" else "nein",
        "anruf_moeglich": "ja" if c.get("callButtonState") == "active" else "nein",
    }


def _euro(text: str) -> Optional[float]:
    m = re.search(r"([\d.]+(?:,\d{2})?)\s*€", text or "")
    if not m:
        return None
    try:
        return float(m.group(1).replace(".", "").replace(",", "."))
    except ValueError:
        return None


def enrich_listing(listing) -> bool:
    """Reichert ein IS24-Listing um Detail-Merkmale, Beschreibung und Warmmiete an.

    Die Trefferliste der API liefert nur die Kaltmiete — die Gesamt-/Warmmiete
    steht erst im Expose. Ohne sie kann eine Warmmieten-Obergrenze im Auftrag
    nicht geprüft werden.

    Gibt True zurück, wenn angereichert wurde. Nur für portal == 'is24'.
    """
    if getattr(listing, "portal", "") != "is24":
        return False
    lid = extract_is24_id(listing.url) or extract_is24_id(listing.id)
    if not lid:
        return False
    data = fetch_expose(lid)
    if not data:
        return False

    feats = parse_features(data)
    if feats:
        listing.merkmale = list(listing.merkmale) + feats

        # Warmmiete/Kaltmiete aus den Detail-Attributen übernehmen
        for f in feats:
            low = f.lower()
            if listing.warmmiete is None and low.startswith(("gesamtmiete:", "warmmiete:")):
                listing.warmmiete = _euro(f)
            elif listing.kaltmiete is None and low.startswith(("kaltmiete", "nettokaltmiete")):
                listing.kaltmiete = _euro(f)

    desc = parse_description(data)
    if desc:
        listing.merkmale.append(f"Beschreibung:{desc}")
    return True


def get_contact(url_or_id: str) -> Dict[str, Optional[str]]:
    """Kontaktdaten zu einem IS24-Inserat. Wirft nicht, liefert immer ein Dict."""
    lid = extract_is24_id(url_or_id)
    if not lid:
        return {"fehler": "keine gültige ImmoScout24-ID/URL erkannt"}
    data = fetch_expose(lid)
    if not data:
        return {"fehler": "Inserat nicht abrufbar (evtl. offline oder gelöscht)"}
    res = parse_contact(data)
    res["titel"] = (data.get("header") or {}).get("title") or ""
    res["url"] = f"https://www.immobilienscout24.de/expose/{lid}"
    return res
