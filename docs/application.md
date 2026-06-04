# Bewerbungsprozess

## Prinzip: halbautomatisch

Der Bot unterstützt die Bewerbung, sendet aber **niemals selbstständig** eine Nachricht an Vermieter. Jede Bewerbung erfordert manuelle Freigabe.

## Ablauf

```
Bot findet Inserat
    │
    ▼
Telegram-Benachrichtigung mit Inline-Buttons:
  [✓ Interessiert]  [✗ Überspringen]
    │
    ▼ Klick auf "Interessiert"
Bot sendet vorausgefüllte Bewerbungsvorlage zur Vorschau
    │
    ▼
[📤 Absenden]  [✎ Bearbeiten]  [✗ Abbrechen]
    │
    ▼ Klick auf "Absenden"
Bot sendet Bewerbung per E-Mail / Portal-Kontaktformular
```

## Bewerbungsvorlage

Vorlage liegt unter `config/application_template.txt` (noch zu erstellen).

Platzhalter:
- `{anrede}` — aus Inserat extrahiert (falls vorhanden)
- `{adresse}` — aus Inserat
- `{verfuegbar_ab}` — aus `criteria.yaml`
- `{name}`, `{email}`, `{telefon}` — aus `.env`

## Versandwege

| Portal | Methode |
|--------|---------|
| IS24 | E-Mail an Anbieter (API) |
| ImmoWelt | E-Mail oder Kontaktformular |
| eBay Kleinanzeigen | Plattform-Nachricht (Playwright) |
| WG-Gesucht | Plattform-Nachricht (Playwright) |

## Tracking

Jede Bewerbung wird in der SQLite-DB gespeichert:
- Inserat-ID, Portal, Datum, Status (`gesendet` / `übersprungen` / `ausstehend`)

Status-Abfrage per Telegram-Befehl: `/status`

## Phase 1 (aktuell geplant)

Nur Benachrichtigung ohne Bewerbungsfunktion. Inline-Buttons und Versand kommen in Phase 2.
