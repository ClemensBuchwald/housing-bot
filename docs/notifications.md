# Benachrichtigungen

## Kanäle

### Telegram (primär)

**Setup:**
1. Bot via @BotFather erstellen → `TELEGRAM_BOT_TOKEN`
2. Chat-ID ermitteln: Bot anschreiben, dann `getUpdates` aufrufen → `TELEGRAM_CHAT_ID`

**Nachrichtenformat:**

```
🏠 Neue Wohnung gefunden!

📍 Prenzlauer Berg, Berlin
💶 1.100 € Warm · 65 m² · 2 Zimmer
📅 Frei ab: 01.08.2026

Balkon ✓  Einbauküche –  Haustiere ✓

🔗 https://www.immobilienscout24.de/...

⭐ Score: 75/100
```

**Limits:**
- Telegram erlaubt max. ~30 Nachrichten/Sekunde an einen Bot.
- Bei vielen Treffern: Batching mit 1s Pause zwischen Nachrichten.

**Fehlerbehandlung:**
- Bei HTTP 429 (Too Many Requests): Retry nach `retry_after` Sekunden.
- Bei Netzwerkfehler: 3 Versuche, dann Fehler in lokales Log schreiben.

---

### E-Mail (optional)

**Bibliothek:** `smtplib` (Standard-Python)

**Format:** Plain-Text analog zur Telegram-Nachricht.

**Aktivierung:** `NOTIFY_EMAIL` in `.env` setzen und `benachrichtigung.email: true` in `criteria.yaml`.

---

## Deduplizierung

Jede gesendete Benachrichtigung wird mit Timestamp in der SQLite-DB markiert.
Wiederholung nur wenn `nur_neue: false` in `criteria.yaml` — dann tägliche Resends möglich.

## Digest-Modus (geplant)

Statt Einzel-Nachrichten: einmal täglich Zusammenfassung aller neuen Treffer.
Konfiguration: `benachrichtigung.modus: digest` (noch nicht implementiert).
