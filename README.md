# Housing Bot

Automatisierter Wohnungs-Suchbot: überwacht Immobilienportale, filtert nach Kriterien und benachrichtigt per Telegram/E-Mail.

## Überblick

- Portale: ImmobilienScout24, ImmoWelt, WG-Gesucht, eBay Kleinanzeigen
- Matching: regelbasiert nach Kriterien aus `config/criteria.yaml`
- Benachrichtigung: Telegram (primär), E-Mail (optional)
- Bewerbung: halbautomatisch (Vorlagen + manuelle Freigabe)
- Deployment: Docker Compose auf eigenem Server

## Projektstruktur

```
housing-bot/
├── docs/               # Architektur, Portale, Matching, Benachrichtigung, Bewerbung, Roadmap
├── config/             # criteria.yaml (Suchkriterien), portal-configs
├── src/                # Quellcode (noch leer)
├── tests/              # Tests (noch leer)
├── docker/             # Dockerfiles und Compose-Konfiguration
├── .env.example        # Vorlage für Umgebungsvariablen
└── CLAUDE.md           # Arbeitsregeln für Claude Code
```

## Schnellstart (geplant)

```bash
cp .env.example .env
# .env mit echten Werten befüllen
docker compose up -d
```

## Dokumentation

Alle Konzeptdokumente liegen unter [docs/](docs/).

- [Architektur](docs/architecture.md)
- [Portale](docs/portals.md)
- [Matching-Logik](docs/matching.md)
- [Benachrichtigungen](docs/notifications.md)
- [Bewerbungsprozess](docs/application.md)
- [Roadmap](docs/roadmap.md)
