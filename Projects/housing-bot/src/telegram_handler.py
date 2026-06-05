"""Telegram-Eingangs-Handler: empfängt Nachrichten und Befehle via getUpdates-Polling.

Zustandsmaschine für Aufträge:
  kein Auftrag  → Freitext = neuer Auftrag wird angelegt
  aktiver Auftrag → /pause, /stop, /status, /auftrag möglich
  pausierter Auftrag → /weiter reaktiviert
"""
from __future__ import annotations

import logging
import os
import time
from typing import Optional

import httpx

from src.evaluator import answer_question, classify_intent, parse_mandate
from src.store import Store

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}"

# Minimale Nachrichtenlänge damit Freitext als Auftrag gilt
_MIN_MANDATE_LENGTH = 20

# Schlüsselwörter die auf eine echte Wohnungssuche hinweisen
_MANDATE_KEYWORDS = [
    "zimmer", "wohnung", "qm", "m²", "miete", "warm", "kalt",
    "suche", "charlottenburg", "wilmersdorf", "halensee", "grunewald",
    "balkon", "terrasse", "erdgeschoss", "aufzug", "keller",
    "bezirk", "ortsteil", "nebenkosten",
]

# Fragen/Befehle die NICHT als Auftrag gewertet werden sollen
_QUESTION_PATTERNS = [
    "was suchst", "was läuft", "wieviele", "wie viele", "was machst",
    "bist du", "hörst du", "kannst du", "zeig mir", "zeige mir",
    "was hast du", "was ist", "erkläre", "hilf mir", "was kann",
]


def _is_mandate(text: str) -> bool:
    """Prüft ob ein Text ein echter Wohnungssuchauftrag ist."""
    lower = text.lower()
    # Fragen ausschließen
    if any(p in lower for p in _QUESTION_PATTERNS):
        return False
    # Mindestens 2 Wohnungs-Schlüsselwörter müssen vorkommen
    hits = sum(1 for kw in _MANDATE_KEYWORDS if kw in lower)
    return hits >= 2


class TelegramHandler:
    def __init__(self, store: Store) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.store = store
        self._offset = 0
        self._base = _TG_API.format(token=self.token)

    def poll_once(self) -> None:
        """Holt neue Updates und verarbeitet sie."""
        if not self.token:
            return
        try:
            resp = httpx.get(
                f"{self._base}/getUpdates",
                params={"offset": self._offset, "timeout": 5},
                timeout=10,
            )
            if not resp.is_success:
                return
            updates = resp.json().get("result", [])
            for update in updates:
                self._offset = update["update_id"] + 1
                self._handle_update(update)
        except Exception as e:
            logger.debug("Telegram-Poll-Fehler: %s", e)

    def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or update.get("edited_message")
        if not msg:
            return
        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "").strip()
        if not text:
            return

        # Nur erlaubte Chat-ID beachten (Sicherheit)
        if self.chat_id and chat_id != self.chat_id:
            logger.info("Nachricht von Chat %s ('%s') ignoriert — nicht in TELEGRAM_CHAT_ID",
                        chat_id, msg.get("chat", {}).get("title", "privat"))
            return

        logger.info("Telegram-Eingang [%s]: %s", chat_id, text[:80])

        if text.startswith("/"):
            self._handle_command(chat_id, text)
        else:
            self._handle_freetext(chat_id, text)

    def _handle_command(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].lower().split("@")[0]

        if cmd == "/start":
            self.send(chat_id, (
                "🏠 *Housing Bot aktiv*\n\n"
                "Ich suche dauerhaft nach Wohnungen — aber erst wenn du mir einen "
                "klaren Suchauftrag gibst.\n\n"
                "Schreib mir einfach was du suchst, z. B.:\n"
                "_3 Zimmer in Charlottenburg, max 1.400 EUR warm, Balkon, kein Erdgeschoss_\n\n"
                "Befehle:\n"
                "/auftrag — aktuellen Auftrag anzeigen\n"
                "/pause — Suche pausieren\n"
                "/weiter — Suche fortsetzen\n"
                "/stop — Suche beenden"
            ))

        elif cmd == "/auftrag":
            mandate = self.store.get_active_mandate(chat_id)
            if not mandate:
                paused = self._get_paused_mandate(chat_id)
                if paused:
                    self.send(chat_id, f"⏸ *Pausierter Auftrag*\n\n_{paused['raw_text']}_\n\nMit /weiter fortsetzen.")
                else:
                    self.send(chat_id,
                        "😴 Kein aktiver Suchauftrag.\n\n"
                        "Schreib mir deinen Auftrag als Freitext, z. B.:\n"
                        "_3 Zimmer in Charlottenburg, max 1.400 € warm, Balkon, kein EG_"
                    )
            else:
                s = mandate.get("structured", {})
                lines = ["✅ *Aktuell aktiver Suchauftrag:*\n"]
                lines.append(f"_{mandate['raw_text']}_\n")
                lines.append("*Verstanden als:*")
                if s.get("zielorte"):
                    lines.append(f"📍 Orte: {', '.join(s['zielorte'])}")
                if s.get("warmmiete_max"):
                    lines.append(f"💶 Max. Warmmiete: {s['warmmiete_max']} €")
                if s.get("zimmer_min"):
                    zi = str(s['zimmer_min'])
                    if s.get("zimmer_max"):
                        zi += f"–{s['zimmer_max']}"
                    lines.append(f"🚪 Zimmer: {zi}")
                if s.get("flaeche_min"):
                    lines.append(f"📐 Mindestfläche: {s['flaeche_min']} m²")
                if s.get("ausschlusskriterien"):
                    lines.append(f"🚫 Ausschluss: {', '.join(s['ausschlusskriterien'])}")
                if s.get("wunschkriterien"):
                    lines.append(f"⭐ Wünsche: {', '.join(s['wunschkriterien'])}")
                lines.append("\n_/pause · /stop · /status_")
                self.send(chat_id, "\n".join(lines))

        elif cmd == "/pause":
            ok = self.store.set_mandate_state(chat_id, "paused")
            self.send(chat_id, "⏸ Suche pausiert." if ok else "Kein aktiver Auftrag zum Pausieren.")

        elif cmd == "/weiter":
            ok = self.store.set_mandate_state(chat_id, "active")
            self.send(chat_id, "▶️ Suche fortgesetzt." if ok else "Kein pausierter Auftrag.")

        elif cmd == "/stop":
            ok = self.store.set_mandate_state(chat_id, "stopped")
            self.send(chat_id, "🛑 Suche gestoppt. Neuen Auftrag jederzeit einfach schreiben." if ok else "Kein aktiver Auftrag.")

        elif cmd == "/status":
            mandate = self.store.get_active_mandate(chat_id)
            if mandate:
                self.send(chat_id, "✅ Bot sucht aktiv.")
            else:
                self.send(chat_id, "😴 Kein aktiver Auftrag — Bot wartet.")

        else:
            self.send(chat_id, f"Unbekannter Befehl: {cmd}")

    def _handle_freetext(self, chat_id: str, text: str) -> None:
        if len(text) < 3:
            return  # Zu kurz, ignorieren

        # Claude klassifiziert den Intent
        intent = classify_intent(text)
        logger.info("[%s] Intent: %s — '%s'", chat_id, intent, text[:60])

        mandate = self.store.get_active_mandate(chat_id)

        if intent == "FRAGE":
            antwort = answer_question(text, mandate)
            self.send(chat_id, antwort)
            return

        if intent == "BEFEHL":
            # Steuerbefehle per Text erkennen
            text_lower = text.lower()
            if any(w in text_lower for w in ["stopp", "stop", "abbrech", "beend"]):
                ok = self.store.set_mandate_state(chat_id, "stopped")
                self.send(chat_id, "🛑 Suche gestoppt." if ok else "Kein aktiver Auftrag.")
            elif any(w in text_lower for w in ["pause", "pausier"]):
                ok = self.store.set_mandate_state(chat_id, "paused")
                self.send(chat_id, "⏸ Suche pausiert." if ok else "Kein aktiver Auftrag.")
            elif any(w in text_lower for w in ["weiter", "fortset", "aktivier", "start"]):
                ok = self.store.set_mandate_state(chat_id, "active")
                self.send(chat_id, "▶️ Suche fortgesetzt." if ok else "Kein pausierter Auftrag.")
            else:
                antwort = answer_question(text, mandate)
                self.send(chat_id, antwort)
            return

        if intent == "SMALL_TALK":
            antwort = answer_question(text, mandate)
            self.send(chat_id, antwort)
            return

        # intent == "MANDAT" — Suchauftrag speichern
        if len(text) < _MIN_MANDATE_LENGTH:
            self.send(chat_id, "Bitte beschreibe deinen Suchauftrag etwas ausführlicher.")
            return

        self.send(chat_id, "⏳ Auftrag wird verstanden… einen Moment.")

        try:
            structured = parse_mandate(text)
        except Exception as e:
            logger.error("Mandate-Parse fehlgeschlagen: %s", e)
            self.send(chat_id, "❌ Auftrag konnte nicht verarbeitet werden. Bitte nochmal versuchen.")
            return

        mandate_id = self.store.save_mandate(chat_id, text, structured)

        # Bestätigung mit geparsten Feldern
        s = structured
        lines = ["✅ *Suchauftrag gespeichert!* Ich suche jetzt dauerhaft.\n"]
        if s.get("zielorte"):
            lines.append(f"📍 Orte: {', '.join(s['zielorte'])}")
        if s.get("warmmiete_max"):
            lines.append(f"💶 Max. Warmmiete: {s['warmmiete_max']} €")
        if s.get("zimmer_min"):
            zi = str(s['zimmer_min'])
            if s.get("zimmer_max"):
                zi += f"–{s['zimmer_max']}"
            lines.append(f"🚪 Zimmer: {zi}")
        if s.get("flaeche_min"):
            lines.append(f"📐 Mindestfläche: {s['flaeche_min']} m²")
        if s.get("ausschlusskriterien"):
            lines.append(f"🚫 Ausschluss: {', '.join(s['ausschlusskriterien'])}")
        if s.get("wunschkriterien"):
            lines.append(f"⭐ Wünsche: {', '.join(s['wunschkriterien'])}")
        if s.get("sonstiges"):
            lines.append(f"📝 Hinweis: {s['sonstiges']}")
        lines.append("\nIch melde mich sobald ich passende Angebote finde!")

        self.send(chat_id, "\n".join(lines))
        logger.info("Neuer Auftrag #%d für Chat %s gespeichert", mandate_id, chat_id)

    def _get_paused_mandate(self, chat_id: str) -> Optional[dict]:
        row = self.store._conn.execute(
            "SELECT * FROM mandates WHERE chat_id = ? AND state = 'paused' ORDER BY id DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None

    def send(self, chat_id: str, text: str) -> None:
        if not self.token:
            logger.info("Telegram (kein Token): %s", text[:100])
            return
        try:
            httpx.post(
                f"{self._base}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
                timeout=10,
            )
        except Exception as e:
            logger.warning("Telegram-Send-Fehler: %s", e)
