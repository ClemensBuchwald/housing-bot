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
from src.store import EVAL_BLOCKED, EVAL_FAILED, EVAL_PENDING, Store

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}"


class TelegramHandler:
    def __init__(self, store: Store, search_fn=None, sources_text: str = "",
                 contact_fn=None, health=None) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.store = store
        self.health = health
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
                # Wurde bisher wortlos verschluckt: Der Bot lief scheinbar
                # weiter, empfing aber dauerhaft nichts mehr.
                self._abruf_fehler(resp.status_code)
                return

            self._abruf_erfolg()
            for update in resp.json().get("result", []):
                self._offset = update["update_id"] + 1
                self._handle_update(update)
        except httpx.HTTPError as e:
            # Netzaussetzer sind normal und kein Grund zur Aufregung —
            # aber sie zählen mit, damit eine Dauerstörung sichtbar wird.
            logger.debug("Telegram-Netzfehler: %s", e)
            self._abruf_fehler(None, "Netzfehler")
        except Exception as e:
            logger.exception("Unerwarteter Fehler beim Telegram-Abruf: %s", e)
            self._abruf_fehler(None, "unerwartet")

    # Statuscodes, die eine Erklärung verdienen. Der Token taucht nirgends auf.
    _CODE_HINWEIS = {
        401: "Token ungültig oder widerrufen — Bot empfängt nichts mehr",
        403: "Bot wurde blockiert oder aus dem Chat entfernt",
        404: "Bot-Endpunkt unbekannt — Token vermutlich falsch",
        409: "Ein zweiter Abrufer ist aktiv (anderer Prozess oder Webhook gesetzt)",
        429: "Telegram drosselt die Abrufe",
    }

    def _abruf_fehler(self, code, ersatz: str = "") -> None:
        hinweis = self._CODE_HINWEIS.get(code, ersatz or "unerwartete Antwort")
        if code in (401, 403, 404, 409):
            logger.error("Telegram-Abruf gescheitert (%s): %s", code, hinweis)
        else:
            logger.warning("Telegram-Abruf gescheitert (%s): %s", code, hinweis)
        if self.health:
            self.health.telegram_fehler(code, hinweis)

    def _abruf_erfolg(self) -> None:
        if self.health:
            self.health.telegram_erfolg()

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
                "bitten, die Suche zu pausieren oder zu stoppen.\n\n"
                "Mit /zustand seht ihr, ob technisch alles läuft."
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
        elif cmd in ("/zustand", "/health"):
            self.send(chat_id, self.zustandsbericht())
        elif cmd in ("/auftrag", "/status"):
            # Über den Agenten beantworten, damit es natürlich klingt
            reply = self.agent.handle(chat_id, "Wonach suchst du gerade?")
            self.send(chat_id, reply)
        else:
            # Unbekannte Slash-Eingabe an den Agenten weiterreichen
            reply = self.agent.handle(chat_id, text)
            self.send(chat_id, reply)

    def zustandsbericht(self) -> str:
        """Technischer Kurzbericht — bewusst OHNE Modellaufruf.

        Gerade wenn der Anbieter ausfällt, muss diese Auskunft noch funktionieren.
        Eine Antwort, die selbst ein LLM braucht, wäre genau dann stumm, wenn man
        sie am dringendsten bräuchte.

        Keine Geheimnisse, keine Stapelabzüge — nur Betriebszustand.
        """
        from src.health import HEALTHY, bewerten

        zeilen = []
        daten = self.health.schnappschuss() if self.health else None
        if daten:
            status, gruende = bewerten(daten)
            symbol = {"healthy": "🟢", "degraded": "🟡"}.get(status, "🔴")
            zeilen.append(f"{symbol} Bot läuft (seit {daten.get('gestartet_am', '?')})")
        else:
            zeilen.append("🟢 Bot läuft")
            status, gruende = HEALTHY, []

        # Suchauftrag
        try:
            aktiv = self.store.get_active_mandate(self.chat_id) or self.store.get_any_active_mandate()
            pausiert = self.store.get_paused_mandate(self.chat_id)
            if aktiv:
                zeilen.append(f"🔎 Suche aktiv: {str(aktiv.get('raw_text', ''))[:60]}")
            elif pausiert:
                zeilen.append("⏸ Suche pausiert")
            else:
                zeilen.append("💤 Kein aktiver Suchauftrag")
        except Exception:
            zeilen.append("🔎 Suchauftrag nicht lesbar")

        # Letzter Zyklus
        zyklus = (daten or {}).get("zyklus")
        if zyklus:
            teile = [f"{zyklus.get('quellen', 0)} Quellen",
                     f"{zyklus.get('inserate', 0)} Inserate",
                     f"{zyklus.get('treffer', 0)} Treffer"]
            if zyklus.get("quellen_fehler"):
                teile.append(f"{zyklus['quellen_fehler']} Quellen gestört")
            zeilen.append(f"🔄 Letzter Durchlauf {zyklus.get('beendet_am', '?')} "
                          f"({', '.join(teile)})")
        else:
            zeilen.append("🔄 Noch kein Durchlauf abgeschlossen")

        # Warteschlange
        try:
            z = self.store.eval_queue_zaehler()
        except Exception:
            z = {}
        offen = z.get(EVAL_PENDING, 0)
        blockiert = z.get(EVAL_BLOCKED, 0)
        if offen or blockiert or z:
            teile = [f"{offen} offen"]
            if blockiert:
                teile.append(f"{blockiert} blockiert")
            if z.get(EVAL_FAILED):
                teile.append(f"{z[EVAL_FAILED]} aufgegeben")
            if z.get("expired"):
                teile.append(f"{z['expired']} verfallen")
            zeilen.append("📋 Wiedervorlage: " + ", ".join(teile))
        else:
            zeilen.append("📋 Wiedervorlage: leer")

        # Modellanbieter
        llm = (daten or {}).get("llm") or {}
        if llm.get("zustand") == "geschlossen":
            zeilen.append("🧠 Bewertung: in Ordnung")
        else:
            grund = llm.get("kategorie") or "unbekannt"
            zeilen.append(f"🧠 Bewertung gestört ({grund})"
                          + (f", Sperre noch {llm['gesperrt_noch_s']}s"
                             if llm.get("gesperrt_noch_s") else ""))

        if status != HEALTHY and gruende:
            zeilen.append("⚠️ " + "; ".join(gruende[:3]))

        return "\n".join(zeilen)

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
