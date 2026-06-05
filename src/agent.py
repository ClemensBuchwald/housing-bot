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
import os
from typing import TYPE_CHECKING, Dict, List

import anthropic

if TYPE_CHECKING:
    from src.store import Store

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5"
_MAX_HISTORY = 24  # letzte N Nachrichten pro Chat behalten

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
- Bei normalem Gespräch antworte einfach natürlich, ohne Tools.

Sobald ein Auftrag aktiv ist, durchsuchst du im Hintergrund Berliner Wohnungsportale \
(landeseigene Gesellschaften, Genossenschaften, Vonovia/Deutsche Wohnen) und meldest \
passende Treffer mit Vor- und Nachteilen samt Empfehlung.

Halt dich kurz — das ist ein Telegram-Chat, kein Brief. Emojis sparsam, wenn sie passen.

WICHTIG bei `jetzt_angebote_suchen`: Die gefundenen Angebote werden bereits AUTOMATISCH
als einzelne Nachrichten mit Links an den Chat gesendet. Zähl die Treffer NICHT selbst auf
und gib KEINE Links wieder — sag nur kurz, wie viele Treffer du geschickt hast (z. B.
"Hier sind 4 aktuelle Angebote ⤴" oder "Gerade keine passenden Angebote gefunden")."""


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
            },
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

    lines = [f"🏢 {quelle}"]
    if titel and titel.lower() not in zeile2.lower():
        lines.append(titel[:70])
    lines.append(zeile2)
    lines.append(preis)
    if url:
        lines.append(url)  # bare URL → klickbar + Telegram-Vorschau
    return "\n".join(lines)


def _client() -> anthropic.Anthropic:
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY fehlt in .env")
    return anthropic.Anthropic(api_key=api_key)


class ConversationAgent:
    def __init__(self, store: "Store", search_fn=None, notify_fn=None) -> None:
        # search_fn(criteria: dict) -> List[dict]: optionale Live-Suchfunktion
        # notify_fn(chat_id: str, text: str): direkter Telegram-Versand (am KI-Text vorbei)
        self.store = store
        self.search_fn = search_fn
        self.notify_fn = notify_fn
        self._histories: Dict[str, List[dict]] = {}

    def handle(self, chat_id: str, text: str) -> str:
        """Verarbeitet eine Nutzernachricht und gibt die Antwort zurück."""
        history = self._histories.setdefault(chat_id, [])
        history.append({"role": "user", "content": text})
        self._trim(history)

        try:
            reply = self._run(chat_id, history)
        except Exception as e:
            logger.exception("Agent-Fehler: %s", e)
            return "Ups, da ist gerade etwas schiefgelaufen. Magst du es nochmal versuchen?"

        history.append({"role": "assistant", "content": reply})
        self._trim(history)
        return reply

    def _run(self, chat_id: str, history: List[dict]) -> str:
        client = _client()
        # Lokale Kopie für die Tool-Use-Schleife (mit rohen Content-Blöcken)
        messages: List[dict] = [{"role": m["role"], "content": m["content"]} for m in history]

        for _ in range(5):  # max. 5 Tool-Runden
            resp = client.messages.create(
                model=_MODEL,
                max_tokens=1024,
                system=_SYSTEM_PROMPT,
                tools=TOOLS,
                messages=messages,
            )

            if resp.stop_reason == "tool_use":
                messages.append({"role": "assistant", "content": resp.content})
                results = []
                for block in resp.content:
                    if block.type == "tool_use":
                        logger.info("[%s] Tool: %s(%s)", chat_id, block.name, block.input)
                        out = self._exec_tool(chat_id, block.name, block.input)
                        results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": out,
                        })
                messages.append({"role": "user", "content": results})
                continue

            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            return text or "Ok!"

        return "Ok!"

    def _exec_tool(self, chat_id: str, name: str, args: dict) -> str:
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
            )

        if name == "aktuellen_auftrag_abrufen":
            m = self.store.get_active_mandate(chat_id)
            if not m:
                p = self._paused(chat_id)
                if p:
                    return json.dumps({"status": "pausiert", "auftrag": p["raw_text"]}, ensure_ascii=False)
                return json.dumps({"status": "kein_auftrag"})
            return json.dumps(
                {"status": "aktiv", "auftrag": m["raw_text"], "details": m.get("structured", {})},
                ensure_ascii=False,
            )

        if name == "suche_pausieren":
            ok = self.store.set_mandate_state(chat_id, "paused")
            return "pausiert" if ok else "kein_aktiver_auftrag"

        if name == "suche_fortsetzen":
            ok = self.store.set_mandate_state(chat_id, "active")
            return "fortgesetzt" if ok else "kein_pausierter_auftrag"

        if name == "suche_stoppen":
            ok = self.store.set_mandate_state(chat_id, "stopped")
            return "gestoppt" if ok else "kein_aktiver_auftrag"

        if name == "jetzt_angebote_suchen":
            if not self.search_fn:
                return json.dumps({"status": "nicht_verfuegbar"})
            criteria = {k: v for k, v in args.items() if v is not None}
            try:
                treffer = self.search_fn(criteria)
            except Exception as e:
                logger.exception("On-Demand-Suche fehlgeschlagen: %s", e)
                return json.dumps({"status": "fehler"})

            # Treffer DIREKT als feste Blöcke senden (am KI-Fließtext vorbei),
            # damit jeder Link klickbar bleibt und nie untergeht.
            if treffer and self.notify_fn:
                for t in treffer:
                    self.notify_fn(chat_id, format_treffer_block(t))

            # Claude bekommt nur die Anzahl zurück → kurze Begleitnachricht, keine Link-Wiedergabe
            return json.dumps({"status": "gesendet", "anzahl": len(treffer)}, ensure_ascii=False)

        return "unbekanntes_tool"

    def _paused(self, chat_id: str) -> dict | None:
        row = self.store._conn.execute(
            "SELECT * FROM mandates WHERE chat_id = ? AND state = 'paused' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None

    def _trim(self, history: List[dict]) -> None:
        if len(history) > _MAX_HISTORY:
            del history[: len(history) - _MAX_HISTORY]
