"""Konversationeller Agent.

Statt starrer Intent-Klassifikation führt Claude eine echte Unterhaltung
mit Gesprächsgedächtnis und steuert die Suche selbst über Tool-Use:
  - suchauftrag_speichern
  - aktuellen_auftrag_abrufen
  - suche_pausieren / suche_fortsetzen / suche_stoppen

Der Agent antwortet natürlich; Aktionen passieren als Nebeneffekt der Tools.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from src.llm import get_provider

if TYPE_CHECKING:
    from src.store import Store

logger = logging.getLogger(__name__)

_MAX_HISTORY = 24  # letzte N Nachrichten pro Chat behalten
_MAX_TOOL_RUNDEN = 5

# Was der Nutzer erfahren muss, wenn die Aktion vollzogen ist, die Antwort des
# Modells darüber aber verloren ging. Der Bot darf weder eine nicht ausgeführte
# Aktion behaupten noch eine ausgeführte verschweigen.
_AKTION_TEXT = {
    "suchauftrag_speichern": "Dein Suchauftrag ist gespeichert — die Suche läuft ab jetzt.",
    "suche_pausieren": "Die Suche ist pausiert.",
    "suche_fortsetzen": "Die Suche läuft wieder.",
    "suche_stoppen": "Die Suche ist gestoppt.",
    "jetzt_angebote_suchen": "Die Sofort-Suche ist durchgelaufen; die Treffer stehen oben im Chat.",
}

_SYSTEM_PROMPT = """\
Du bist der Housing-Bot — ein freundlicher, natürlicher Wohnungssuch-Assistent für \
Clemens und Lana, die zusammen eine Wohnung in Berlin suchen.

Du führst eine ECHTE Konversation. Antworte locker, menschlich und auf Deutsch — \
niemals steif oder formularhaft. Du darfst Rückfragen stellen, Vorschläge machen \
und mitdenken.

Was du kannst:
- Wenn die beiden konkrete Suchkriterien nennen (Ort, Zimmer, Miete, Größe, \
Ausstattung), speicherst du den Auftrag mit dem Tool `suchauftrag_speichern`. \
Wenn etwas Wichtiges fehlt oder unklar ist, frag lieber kurz nach statt zu raten.
- Wenn sie wissen wollen, wonach du gerade suchst, ruf `aktuellen_auftrag_abrufen` \
auf und erzähl es ihnen in eigenen Worten.
- Wenn sie pausieren, fortsetzen oder abbrechen wollen, nutze die passenden Tools.
- Wenn sie nach Kontakt/Ansprechpartner/Makler/Telefon zu einer Wohnung fragen, nutze \
`kontaktdaten_recherchieren` mit der Inserats-URL oder -ID. Gib dann Ansprechpartner, Firma \
und Kontaktweg an. Ist keine Telefonnummer hinterlegt, sag das ehrlich und verweise auf das \
Kontaktformular im Inserat — erfinde NIE Telefonnummern oder E-Mail-Adressen.
- Bei normalem Gespräch antworte einfach natürlich, ohne Tools.

Sobald ein Auftrag aktiv ist, durchsuchst du im Hintergrund diese Quellen und meldest \
passende Treffer mit Vor- und Nachteilen samt Empfehlung.

DEINE AKTIVEN QUELLEN (nur diese nennen, keine anderen erfinden, keine als fehlend behaupten):
{quellen}

Halt dich kurz — das ist ein Telegram-Chat, kein Brief. Emojis sparsam, wenn sie passen.

WICHTIG bei `jetzt_angebote_suchen`: Die Angebote werden bereits gegen die Kriterien
GEPRÜFT und nur die passenden automatisch als einzelne Nachrichten mit Bewertung
(Score, Vor-/Nachteile) und Link an den Chat gesendet. Zähl die Treffer NICHT selbst auf,
gib KEINE Links/Details wieder, fordere den Nutzer NICHT auf selbst zu prüfen —
sag nur kurz, wie viele geprüfte Treffer du geschickt hast (z. B. "Ich habe 4 passende
Angebote geprüft und geschickt ⤴" oder "Gerade nichts dabei, das wirklich passt").
Standardmäßig zeigst du nur NEUE Angebote. Wenn der Nutzer "zeig mir alle", "nochmal alle"
oder "auch die schon gesehenen" sagt, setze auch_bereits_gesehene=true."""


TOOLS = [
    {
        "name": "suchauftrag_speichern",
        "description": (
            "Speichert einen neuen Wohnungs-Suchauftrag und startet die dauerhafte Suche. "
            "Nur aufrufen, wenn der Nutzer konkrete Suchkriterien genannt hat. "
            "Ersetzt einen evtl. vorherigen Auftrag."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "zusammenfassung": {
                    "type": "string",
                    "description": "Der komplette Suchauftrag in 1-2 natürlichen Sätzen",
                },
                "zielorte": {"type": "array", "items": {"type": "string"},
                             "description": "Gewünschte Stadtteile/Orte"},
                "warmmiete_max": {"type": ["number", "null"]},
                "kaltmiete_max": {"type": ["number", "null"]},
                "zimmer_min": {"type": ["number", "null"]},
                "zimmer_max": {"type": ["number", "null"]},
                "flaeche_min": {"type": ["number", "null"]},
                "ausschlusskriterien": {"type": "array", "items": {"type": "string"}},
                "wunschkriterien": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["zusammenfassung"],
        },
    },
    {
        "name": "aktuellen_auftrag_abrufen",
        "description": "Ruft den aktuell aktiven oder pausierten Suchauftrag ab.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "jetzt_angebote_suchen",
        "description": (
            "Macht eine EINMALIGE Live-Abfrage der Portale und zeigt, was es JETZT GERADE gibt — "
            "OHNE einen dauerhaften Suchauftrag anzulegen. Nutze das, wenn der Nutzer den "
            "aktuellen Stand sehen will ('was gibt es gerade', 'zeig mal'). Dauert ca. 20-30 Sek. "
            "Kriterien sind optional; ohne Kriterien werden alle aktuellen Treffer im Zielgebiet gezeigt."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "warmmiete_max": {"type": ["number", "null"]},
                "kaltmiete_max": {"type": ["number", "null"]},
                "zimmer_min": {"type": ["number", "null"]},
                "zimmer_max": {"type": ["number", "null"]},
                "flaeche_min": {"type": ["number", "null"]},
                "auch_bereits_gesehene": {
                    "type": "boolean",
                    "description": (
                        "false (Standard) = nur NEUE, noch nicht gezeigte Angebote. "
                        "true = ALLE passenden inkl. bereits gezeigter — setze das, wenn der "
                        "Nutzer 'zeig mir alle', 'nochmal alle', 'auch die schon gesehenen' o.ä. sagt."
                    ),
                },
            },
        },
    },
    {
        "name": "kontaktdaten_recherchieren",
        "description": (
            "Recherchiert die öffentlich im Inserat hinterlegten Anbieter-/Kontaktdaten "
            "(Ansprechpartner, Firma, Telefon falls angegeben, Kontaktweg) zu einer konkreten "
            "Wohnung. Nutze das, wenn der Nutzer nach Kontakt, Ansprechpartner, Makler, "
            "Telefonnummer oder 'wen kann ich anrufen/anschreiben' fragt. "
            "Übergib die Inserats-URL oder die ImmoScout24-ID. Funktioniert derzeit für "
            "ImmoScout24-Inserate; bei anderen Portalen gib dem Nutzer den Inseratslink."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "listing_url_oder_id": {
                    "type": "string",
                    "description": "Inserats-URL (z.B. https://www.immobilienscout24.de/expose/170346126) oder die Scout-ID",
                },
            },
            "required": ["listing_url_oder_id"],
        },
    },
    {
        "name": "suche_pausieren",
        "description": "Pausiert die laufende Suche vorübergehend.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "suche_fortsetzen",
        "description": "Setzt eine pausierte Suche fort.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "suche_stoppen",
        "description": "Stoppt und löscht den aktiven Suchauftrag komplett.",
        "input_schema": {"type": "object", "properties": {}},
    },
]


def format_treffer_block(t: dict) -> str:
    """Ein Treffer = ein scanbarer Block mit klickbarem Klartext-Link (Telegram-Preview)."""
    quelle = (t.get("quelle") or "").upper()
    ort = t.get("ort") or "Berlin"
    zi = t.get("zimmer")
    fl = t.get("flaeche")
    warm = t.get("warmmiete")
    kalt = t.get("kaltmiete")

    fakten = []
    if zi:
        fakten.append(f"{int(zi) if float(zi).is_integer() else zi} Zi")
    if fl:
        fakten.append(f"{int(fl) if float(fl).is_integer() else fl} m²")
    fakten.append(ort)
    zeile2 = " · ".join(fakten)

    if warm:
        preis = f"{int(warm)} € warm"
    elif kalt:
        preis = f"{int(kalt)} € kalt"
    else:
        preis = "Preis k. A."

    titel = (t.get("titel") or "").strip()
    url = t.get("url") or ""
    score = t.get("score")

    # Fakten-Titel (nur Zimmer/m²/Ort) nicht doppelt zeigen
    titel_ist_fakten = bool(re.search(r"\d\s*Zi", titel)) and "m²" in titel

    kopf = f"🏢 {quelle}"
    if score is not None:
        kopf += f"  ·  Score {score}/100"

    lines = [kopf]
    if titel and not titel_ist_fakten and titel.lower() not in zeile2.lower():
        lines.append(titel[:70])
    lines.append(zeile2)
    lines.append(preis)

    for v in (t.get("vorteile") or [])[:2]:
        lines.append(f"✅ {v}")
    for n in (t.get("nachteile") or [])[:2]:
        lines.append(f"⚠️ {n}")
    if t.get("empfehlung"):
        lines.append(f"→ {t['empfehlung']}")

    if url:
        lines.append(url)  # bare URL → klickbar + Telegram-Vorschau
    return "\n".join(lines)


# Menschenlesbare Beschreibung je Scraper-Name (für die Quellen-Auskunft des Bots)
SOURCE_LABELS = {
    "inberlinwohnen": "Berliner landeseigene Gesellschaften (degewo, GESOBAU, Gewobag, HOWOGE, STADT UND LAND, WBM) über inberlinwohnen.de",
    "wbm": "WBM (landeseigen, direkt)",
    "gesobau": "GESOBAU (landeseigen, direkt)",
    "gewobag": "Gewobag (landeseigen, direkt)",
    "degewo": "degewo (landeseigen, direkt)",
    "vonovia": "Vonovia / Deutsche Wohnen (größter privater Vermieter)",
    "is24": "ImmobilienScout24 (größtes Portal)",
    "immowelt": "Immowelt",
    "kleinanzeigen": "eBay Kleinanzeigen (private Anbieter + Makler)",
    "charlotte": "Charlottenburger Baugenossenschaft (CHARLOTTE) — Genossenschaftswohnungen, Mitgliedschaft für die Bewerbung nötig",
}


def build_sources_text(scraper_names: List[str]) -> str:
    seen = set()
    lines = []
    for n in scraper_names:
        label = SOURCE_LABELS.get(n, n)
        if label not in seen:
            seen.add(label)
            lines.append(f"- {label}")
    return "\n".join(lines)


class ConversationAgent:
    def __init__(self, store: "Store", search_fn=None, notify_fn=None, sources_text: str = "",
                 contact_fn=None) -> None:
        # search_fn(criteria: dict) -> List[dict]: optionale Live-Suchfunktion
        # notify_fn(chat_id: str, text: str): direkter Telegram-Versand (am KI-Text vorbei)
        # sources_text: menschenlesbare Liste der aktiven Quellen (für korrekte Auskunft)
        # contact_fn(url_or_id: str) -> dict: Kontaktdaten-Recherche zum Inserat
        self.store = store
        self.search_fn = search_fn
        self.notify_fn = notify_fn
        self.contact_fn = contact_fn
        self.sources_text = sources_text or "- (Quellen werden geladen)"
        self._histories: Dict[str, List[dict]] = {}

    def handle(self, chat_id: str, text: str) -> str:
        """Verarbeitet eine Nutzernachricht und gibt die Antwort zurück."""
        # Verlauf beim ersten Mal aus dem Store laden (überlebt Neustarts)
        if chat_id not in self._histories:
            try:
                self._histories[chat_id] = self.store.get_chat_history(chat_id, limit=_MAX_HISTORY)
            except Exception:
                self._histories[chat_id] = []

        history = self._histories[chat_id]
        history.append({"role": "user", "content": text})
        self._trim(history)
        self._persist(chat_id, "user", text)

        # Was während dieses Zuges TATSÄCHLICH vollzogen wurde. Entscheidend für
        # den Fehlerfall: Bricht das Modell ab, nachdem ein Werkzeug bereits
        # gelaufen ist, ist die Aktion trotzdem passiert — der Nutzer darf dann
        # kein pauschales "hat nicht geklappt" bekommen.
        ausgefuehrt: List[str] = []
        try:
            reply = self._run(chat_id, history, ausgefuehrt)
        except Exception as e:
            logger.exception("Agent-Fehler: %s", e)
            reply = self._fehler_text(ausgefuehrt)

        history.append({"role": "assistant", "content": reply})
        self._trim(history)
        self._persist(chat_id, "assistant", reply)
        return reply

    def _persist(self, chat_id: str, role: str, content: str) -> None:
        try:
            self.store.append_chat(chat_id, role, content)
        except Exception:
            logger.debug("Chat-Persistenz fehlgeschlagen", exc_info=True)

    def _fehler_text(self, ausgefuehrt: List[str]) -> str:
        """Ehrliche Antwort nach einem Abbruch — abhängig davon, was schon geschah."""
        if not ausgefuehrt:
            return "Ups, da ist gerade etwas schiefgelaufen. Magst du es nochmal versuchen?"
        zeilen = [_AKTION_TEXT.get(a, f"Aktion „{a}“ wurde ausgeführt.") for a in ausgefuehrt]
        zeilen.append("Meine Antwort dazu ist leider abgebrochen — die Aktion selbst hat aber geklappt.")
        return "\n".join(zeilen)

    def _abgeschnitten_text(self, ausgefuehrt: List[str]) -> str:
        """Antwort lief ins Token-Limit.

        Der angefangene Text darf NICHT ausgeliefert werden: Er endet oft mitten
        in einer Ankündigung ("Ich speichere dir das gleich ab …"), für die nie
        ein Werkzeugaufruf kam. Genau daraus entstünde die Behauptung einer
        Aktion, die nie stattgefunden hat.
        """
        if ausgefuehrt:
            zeilen = [_AKTION_TEXT.get(a, f"Aktion „{a}“ wurde ausgeführt.") for a in ausgefuehrt]
            zeilen.append("Meine Erklärung dazu wurde abgeschnitten — frag gern nochmal nach.")
            return "\n".join(zeilen)
        return ("Meine Antwort wurde leider abgeschnitten, bevor ich fertig war — "
                "und ich habe dabei nichts gespeichert oder geändert. "
                "Magst du es nochmal versuchen, am besten etwas kürzer gefasst?")

    def _run(self, chat_id: str, history: List[dict], ausgefuehrt: List[str]) -> str:
        provider = get_provider()
        # Lokale Kopie für die Tool-Use-Schleife (mit rohen Content-Blöcken)
        messages: List[dict] = [{"role": m["role"], "content": m["content"]} for m in history]

        for _ in range(_MAX_TOOL_RUNDEN):
            result = provider.complete(
                messages=messages,
                system=_SYSTEM_PROMPT.format(quellen=self.sources_text),
                tools=TOOLS,
                max_tokens=1024,
            )

            if result.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": result.raw_content})
                results = []
                for call in result.tool_calls:
                    logger.info("[%s] Tool: %s(%s)", chat_id, call.name, call.input)
                    out, vollzogen = self._exec_tool(chat_id, call.name, call.input)
                    if vollzogen:
                        ausgefuehrt.append(vollzogen)
                    results.append({
                        "type": "tool_result",
                        "tool_use_id": call.id,
                        "content": out,
                    })
                messages.append({"role": "user", "content": results})
                continue

            if result.stop_reason == "max_tokens":
                logger.warning("[%s] Antwort am Token-Limit abgeschnitten", chat_id)
                return self._abgeschnitten_text(ausgefuehrt)

            text = result.text.strip()
            if text:
                return text
            # Leerer Abschluss ohne Werkzeugaufruf: lieber ehrlich sein als "Ok!"
            return self._abgeschnitten_text(ausgefuehrt) if ausgefuehrt else \
                "Da habe ich gerade keine Antwort zustande gebracht — magst du es nochmal versuchen?"

        logger.warning("[%s] Werkzeug-Schleife nach %d Runden beendet", chat_id, _MAX_TOOL_RUNDEN)
        return self._fehler_text(ausgefuehrt) if ausgefuehrt else \
            "Das hat gerade zu viele Zwischenschritte gebraucht — magst du es nochmal versuchen?"

    def _exec_tool(self, chat_id: str, name: str, args: dict) -> Tuple[str, Optional[str]]:
        """Führt ein Werkzeug aus.

        Rückgabe: (Ergebnis für das Modell, tatsächlich vollzogene Aktion oder None).
        Der zweite Wert ist nur gesetzt, wenn wirklich etwas passiert ist — er
        trägt die Wahrheit, falls das Modell danach ausfällt.
        """
        if name == "suchauftrag_speichern":
            structured = {
                "zielorte": args.get("zielorte", []),
                "warmmiete_max": args.get("warmmiete_max"),
                "kaltmiete_max": args.get("kaltmiete_max"),
                "zimmer_min": args.get("zimmer_min"),
                "zimmer_max": args.get("zimmer_max"),
                "flaeche_min": args.get("flaeche_min"),
                "ausschlusskriterien": args.get("ausschlusskriterien", []),
                "wunschkriterien": args.get("wunschkriterien", []),
            }
            zusammenfassung = args.get("zusammenfassung", "Wohnungssuche")
            mandate_id = self.store.save_mandate(chat_id, zusammenfassung, structured)
            logger.info("Auftrag #%d via Agent gespeichert", mandate_id)
            return json.dumps(
                {"status": "gespeichert", "id": mandate_id, "details": structured},
                ensure_ascii=False,
            ), "suchauftrag_speichern"

        if name == "aktuellen_auftrag_abrufen":
            m = self.store.get_active_mandate(chat_id)
            if not m:
                p = self.store.get_paused_mandate(chat_id)
                if p:
                    return json.dumps({"status": "pausiert", "auftrag": p["raw_text"]},
                                      ensure_ascii=False), None
                return json.dumps({"status": "kein_auftrag"}), None
            return json.dumps(
                {"status": "aktiv", "auftrag": m["raw_text"], "details": m.get("structured", {})},
                ensure_ascii=False,
            ), None

        if name == "kontaktdaten_recherchieren":
            if not self.contact_fn:
                return json.dumps({"status": "nicht_verfuegbar"}), None
            ref = str(args.get("listing_url_oder_id", "")).strip()
            try:
                info = self.contact_fn(ref)
            except Exception as e:
                logger.exception("Kontaktrecherche fehlgeschlagen: %s", e)
                return json.dumps({"status": "fehler"}), None
            return json.dumps({"status": "ok", "kontakt": info}, ensure_ascii=False), None

        if name == "suche_pausieren":
            ok = self.store.set_mandate_state(chat_id, "paused")
            return ("pausiert", "suche_pausieren") if ok else ("kein_aktiver_auftrag", None)

        if name == "suche_fortsetzen":
            ok = self.store.set_mandate_state(chat_id, "active")
            return ("fortgesetzt", "suche_fortsetzen") if ok else ("kein_pausierter_auftrag", None)

        if name == "suche_stoppen":
            ok = self.store.set_mandate_state(chat_id, "stopped")
            return ("gestoppt", "suche_stoppen") if ok else ("kein_aktiver_auftrag", None)

        if name == "jetzt_angebote_suchen":
            if not self.search_fn:
                return json.dumps({"status": "nicht_verfuegbar"}), None
            include_seen = bool(args.get("auch_bereits_gesehene", False))
            criteria = {k: v for k, v in args.items()
                        if v is not None and k != "auch_bereits_gesehene"}
            # Aktiven Auftrag mitgeben, damit die KI gegen die echten Kriterien bewertet
            mandate = self.store.get_active_mandate(chat_id)
            try:
                treffer = self.search_fn(criteria, mandate, include_seen=include_seen)
            except Exception as e:
                logger.exception("On-Demand-Suche fehlgeschlagen: %s", e)
                return json.dumps({"status": "fehler"}), None

            # Treffer DIREKT als feste Blöcke senden (am KI-Fließtext vorbei),
            # damit jeder Link klickbar bleibt und nie untergeht.
            gesendet = 0
            if treffer and self.notify_fn:
                for t in treffer:
                    self.notify_fn(chat_id, format_treffer_block(t))
                    gesendet += 1

            # Claude bekommt nur die Anzahl zurück → kurze Begleitnachricht, keine Link-Wiedergabe
            return json.dumps({"status": "gesendet", "anzahl": len(treffer)},
                              ensure_ascii=False), ("jetzt_angebote_suchen" if gesendet else None)

        return "unbekanntes_tool", None

    def _trim(self, history: List[dict]) -> None:
        if len(history) > _MAX_HISTORY:
            del history[: len(history) - _MAX_HISTORY]
