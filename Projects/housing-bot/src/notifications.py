"""Telegram-Benachrichtigungen."""
from __future__ import annotations

import logging
import os
import time

import httpx

from src.models import MatchResult

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def _format_message(result: MatchResult) -> str:
    l = result.listing

    preis_str = "–"
    if l.warmmiete:
        preis_str = f"{l.warmmiete:.0f} € Warm"
    elif l.kaltmiete:
        preis_str = f"{l.kaltmiete:.0f} € Kalt"

    flaeche_str = f"{l.flaeche:.0f} m²" if l.flaeche else "–"
    zimmer_str = f"{l.zimmer_gerundet} Zi." if l.zimmer else "–"

    ort_str = f"{l.stadtteil}, {l.stadt}" if l.stadtteil else l.stadt

    einzug_str = l.verfuegbar_ab.strftime("%d.%m.%Y") if l.verfuegbar_ab else "sofort"

    balkon = "✓" if l.hat_merkmal("balkon", "terrasse", "loggia") else "–"
    ekueche = "✓" if l.hat_merkmal("einbauküche", "einbaukueche", "ek") else "–"
    haustiere = "✓" if l.hat_merkmal("haustier", "haustiere erlaubt", "hunde", "katzen") else "–"

    return (
        f"🏠 *Neue Wohnung gefunden!*\n"
        f"\n"
        f"📍 {ort_str}\n"
        f"💶 {preis_str} · {flaeche_str} · {zimmer_str}\n"
        f"📅 Frei ab: {einzug_str}\n"
        f"\n"
        f"Balkon {balkon}  Einbauküche {ekueche}  Haustiere {haustiere}\n"
        f"\n"
        f"🔗 {l.url}\n"
        f"\n"
        f"⭐ Score: {result.score}/65"
    )


def _send_telegram(token: str, chat_id: str, text: str, retries: int = 3) -> bool:
    url = TELEGRAM_API.format(token=token)
    for attempt in range(1, retries + 1):
        try:
            resp = httpx.post(
                url,
                json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
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

    def _telegram_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def send(self, result: MatchResult) -> bool:
        if not self._telegram_configured():
            logger.warning(
                "Telegram nicht konfiguriert. Setze TELEGRAM_BOT_TOKEN und "
                "TELEGRAM_CHAT_ID in .env. Nachricht wird nur geloggt."
            )
            logger.info("Treffer: %s — %s", result.listing.titel, result.listing.url)
            return False

        text = _format_message(result)
        ok = _send_telegram(self.token, self.chat_id, text)
        if ok:
            time.sleep(1)  # Höflichkeitspause zwischen Nachrichten
        return ok
