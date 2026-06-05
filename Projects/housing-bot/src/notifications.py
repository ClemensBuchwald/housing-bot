"""Telegram-Benachrichtigungen — mit KI-Bewertung (Evaluation)."""
from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING, Optional

import httpx

from src.models import Listing

if TYPE_CHECKING:
    from src.evaluator import Evaluation

logger = logging.getLogger(__name__)

_TG_API = "https://api.telegram.org/bot{token}/sendMessage"

_EMPFEHLUNG_EMOJI = {
    "sofort anschauen": "🔥",
    "beobachten": "👀",
    "überspringen": "⏭",
}


def format_evaluation_message(listing: Listing, evaluation: "Evaluation") -> str:
    """Formatiert die Telegram-Nachricht mit KI-Bewertung."""
    preis = "–"
    if listing.warmmiete:
        preis = f"{listing.warmmiete:.0f} € Warm"
    elif listing.kaltmiete:
        preis = f"{listing.kaltmiete:.0f} € Kalt"

    flaeche = f"{listing.flaeche:.0f} m²" if listing.flaeche else "–"
    zimmer = f"{listing.zimmer_gerundet} Zi." if listing.zimmer else "–"
    ort = f"{listing.stadtteil}, {listing.stadt}" if listing.stadtteil else listing.stadt
    empfehlung_emoji = _EMPFEHLUNG_EMOJI.get(evaluation.empfehlung, "📋")

    lines = [
        f"🏠 *Neue Wohnung — Score {evaluation.score}/100*",
        "",
        f"📍 {ort}",
        f"💶 {preis} · {flaeche} · {zimmer}",
        f"🏢 {listing.portal.upper()} · [{listing.titel[:50]}]({listing.url})",
        "",
        f"💬 _{evaluation.kurzfazit}_",
        "",
    ]

    if evaluation.vorteile:
        lines.append("✅ *Vorteile*")
        for v in evaluation.vorteile[:4]:
            lines.append(f"  • {v}")
        lines.append("")

    if evaluation.nachteile:
        lines.append("⚠️ *Nachteile*")
        for n in evaluation.nachteile[:3]:
            lines.append(f"  • {n}")
        lines.append("")

    if evaluation.offene_punkte:
        lines.append("❓ *Offene Punkte*")
        for p in evaluation.offene_punkte[:2]:
            lines.append(f"  • {p}")
        lines.append("")

    lines.append(f"{empfehlung_emoji} *Empfehlung: {evaluation.empfehlung.upper()}*")

    return "\n".join(lines)


def _send_telegram(token: str, chat_id: str, text: str, retries: int = 3) -> bool:
    url = _TG_API.format(token=token)
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown",
                      # Link-Vorschau aktiv: Telegram zieht Objektbild/Titel vom Inseratslink
                      "disable_web_page_preview": False},
                timeout=10,
            )
            if resp.status_code == 429:
                retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                logger.warning("Telegram rate limit, warte %ds", retry_after)
                time.sleep(retry_after)
                continue
            resp.raise_for_status()
            return True
        except httpx.HTTPError as e:
            logger.warning("Telegram-Fehler (Versuch %d/%d): %s", attempt, retries, e)
            if attempt < retries:
                time.sleep(2 ** attempt)
    logger.error("Telegram-Nachricht konnte nicht gesendet werden nach %d Versuchen", retries)
    return False


class NotificationService:
    def __init__(self) -> None:
        self.token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    def _configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send_evaluation(self, listing: Listing, evaluation: "Evaluation") -> bool:
        """Sendet eine Benachrichtigung mit KI-Bewertung."""
        if not self._configured():
            logger.warning("Telegram nicht konfiguriert — Treffer nur geloggt.")
            logger.info("TREFFER: %s — Score %d — %s", listing.titel, evaluation.score, evaluation.empfehlung)
            return False

        text = format_evaluation_message(listing, evaluation)
        ok = _send_telegram(self.token, self.chat_id, text)
        if ok:
            time.sleep(1)
        return ok

    def send_text(self, text: str, chat_id: Optional[str] = None) -> bool:
        """Sendet einen einfachen Text (für Status-Meldungen)."""
        cid = chat_id or self.chat_id
        if not self._configured() or not cid:
            logger.info("Telegram (unkonfiguriert): %s", text[:100])
            return False
        return _send_telegram(self.token, cid, text)
