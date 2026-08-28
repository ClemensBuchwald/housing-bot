"""Telegram-Eingangs-Handler: empfängt Nachrichten via getUpdates-Polling.

Freitext läuft komplett über den ConversationAgent (echte Unterhaltung + Tool-Use).
Slash-Befehle bleiben als schnelle Abkürzungen erhalten.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

from src.agent import ConversationAgent
from src.store import Store

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}"


class TelegramHandler:
    def __init__(self, store: Store, search_fn=None, sources_text: str = "",
                 contact_fn=None) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.store = store
        self.agent = ConversationAgent(store, search_fn=search_fn, notify_fn=self.send,
                                       sources_text=sources_text, contact_fn=contact_fn)
        self._offset = 0
        self._base = _TG_API.format(token=self.token)

    def poll_once(self) -> None:
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
            for update in resp.json().get("result", []):
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

        if self.chat_id and chat_id != self.chat_id:
            logger.info("Nachricht von Chat %s ('%s') ignoriert — nicht in TELEGRAM_CHAT_ID",
                        chat_id, msg.get("chat", {}).get("title", "privat"))
            return

        logger.info("Telegram-Eingang [%s]: %s", chat_id, text[:80])

        if text.startswith("/"):
            self._handle_command(chat_id, text)
        else:
            # Echte Konversation über den Agenten
            self._send_typing(chat_id)
            reply = self.agent.handle(chat_id, text)
            self.send(chat_id, reply)

    def _handle_command(self, chat_id: str, text: str) -> None:
        cmd = text.split()[0].lower().split("@")[0]

        if cmd == "/start":
            self.send(chat_id, (
                "Hi! 🏠 Ich bin euer Wohnungssuch-Bot.\n\n"
                "Erzählt mir einfach in normalen Worten, was ihr sucht — z. B. "
                "\"3 Zimmer in Charlottenburg, möglichst mit Balkon, max 1.400 € warm\". "
                "Ich frage nach, wenn etwas unklar ist, und suche dann dauerhaft für euch.\n\n"
                "Ihr könnt mich jederzeit fragen, wonach ich gerade suche, oder mich "
                "bitten, die Suche zu pausieren oder zu stoppen."
            ))
        elif cmd in ("/pause",):
            ok = self.store.set_mandate_state(chat_id, "paused")
            self.send(chat_id, "⏸ Suche pausiert." if ok else "Es läuft gerade keine Suche.")
        elif cmd in ("/weiter", "/resume"):
            ok = self.store.set_mandate_state(chat_id, "active")
            self.send(chat_id, "▶️ Suche fortgesetzt." if ok else "Es gibt keinen pausierten Auftrag.")
        elif cmd in ("/stop",):
            ok = self.store.set_mandate_state(chat_id, "stopped")
            self.send(chat_id, "🛑 Suche gestoppt." if ok else "Es läuft gerade keine Suche.")
        elif cmd in ("/auftrag", "/status"):
            # Über den Agenten beantworten, damit es natürlich klingt
            reply = self.agent.handle(chat_id, "Wonach suchst du gerade?")
            self.send(chat_id, reply)
        else:
            # Unbekannte Slash-Eingabe an den Agenten weiterreichen
            reply = self.agent.handle(chat_id, text)
            self.send(chat_id, reply)

    def _send_typing(self, chat_id: str) -> None:
        if not self.token:
            return
        try:
            httpx.post(f"{self._base}/sendChatAction",
                       json={"chat_id": chat_id, "action": "typing"}, timeout=5)
        except Exception:
            pass

    def send(self, chat_id: str, text: str) -> None:
        """Sendet Text. Fällt bei Markdown-Fehlern auf Klartext zurück."""
        if not self.token:
            logger.info("Telegram (kein Token): %s", text[:120])
            return
        # Erst mit Markdown versuchen, bei 400 ohne parse_mode wiederholen
        for parse_mode in ("Markdown", None):
            try:
                payload = {"chat_id": chat_id, "text": text}
                if parse_mode:
                    payload["parse_mode"] = parse_mode
                resp = httpx.post(f"{self._base}/sendMessage", json=payload, timeout=10)
                if resp.is_success:
                    return
                if resp.status_code != 400:
                    return  # anderer Fehler, nicht Markdown-bedingt
            except Exception as e:
                logger.warning("Telegram-Send-Fehler: %s", e)
                return
