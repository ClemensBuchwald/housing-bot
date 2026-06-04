# Telegram-Setup

## 1. Bot erstellen

1. Telegram öffnen und [@BotFather](https://t.me/BotFather) suchen
2. `/newbot` senden
3. Namen wählen (z. B. `MeinHousingBot`)
4. Username wählen, muss auf `bot` enden (z. B. `mein_housing_bot`)
5. BotFather antwortet mit dem Token:
   ```
   Dein Bot-Token: 123456789:ABCdef-xyz...
   ```
6. Token in `.env` eintragen:
   ```
   TELEGRAM_BOT_TOKEN=123456789:ABCdef-xyz...
   ```

## 2. Chat-ID ermitteln

1. Deinen neuen Bot in Telegram anschreiben (irgendeine Nachricht senden)
2. Im Browser aufrufen:
   ```
   https://api.telegram.org/bot<DEIN_TOKEN>/getUpdates
   ```
3. In der JSON-Antwort die `chat.id` ablesen:
   ```json
   {"message": {"chat": {"id": 987654321, ...}}}
   ```
4. In `.env` eintragen:
   ```
   TELEGRAM_CHAT_ID=987654321
   ```

## 3. Lokal testen

```bash
# Mit Mock-Daten (kein echtes Portal, nutzt Telegram)
python -m src.main --once --mock
```

Erwartetes Ergebnis: 3 Telegram-Nachrichten (Prenzlauer Berg, Friedrichshain, Neukölln).

## 4. Ohne Token (nur Logging)

Wenn `TELEGRAM_BOT_TOKEN` oder `TELEGRAM_CHAT_ID` fehlen, läuft der Bot ohne Absturz.
Treffer werden nur ins Terminal geloggt:

```
WARNING  Telegram nicht konfiguriert. Setze TELEGRAM_BOT_TOKEN und TELEGRAM_CHAT_ID in .env.
INFO     Treffer: Schöne 2-Zimmer-Wohnung in Prenzlauer Berg — https://example.com/inserat/1
```

Das ist nützlich für erste Tests ohne Telegram-Setup.

## 5. Troubleshooting

| Problem | Lösung |
|---------|--------|
| `Unauthorized` | Token falsch oder abgelaufen → neuen Token bei BotFather holen |
| `chat not found` | Chat-ID falsch oder Bot wurde noch nicht angeschrieben |
| Keine Antwort bei `getUpdates` | Bot zuerst in Telegram anschreiben, dann `getUpdates` nochmal |
| Nachrichten kommen doppelt | `nur_neue: true` in `criteria.yaml` — SQLite-DB (`data/`) prüfen |
