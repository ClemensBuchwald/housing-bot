# Housing Bot — Claude Code Arbeitsregeln

Arbeite ausschließlich in diesem Repo:

/Users/cleur_group/Projects/housing-bot

Produktionspfad (nur nach expliziter Freigabe): /srv/housing-bot/

## Autonomie für docs-only Aufgaben

Für klare docs-only Aufgaben darfst du autonom arbeiten:

- git status prüfen
- Dateien unter docs/ und config/ bearbeiten
- gezielt adden
- committen
- pushen
- STATUSBLOCK ausgeben

## Nicht ohne separate Freigabe

Du darfst NICHT ohne separate Freigabe:

- deployen (weder lokal noch auf dem Server)
- Docker-Container starten, stoppen oder rebuilden
- Server, Nginx, DNS oder Domain ändern
- .env oder Secrets anfassen
- API-Keys anzeigen oder speichern
- echte E-Mails oder Benachrichtigungen senden
- Portale oder externe Dienste mit echten Credentials ansprechen
- Datenbankmigrationen ausführen
- Code außerhalb von docs/ und config/ ändern, außer es wurde ausdrücklich beauftragt
- fleur-pa oder andere Repos berühren

## Standardregel

Wenn es ausschließlich docs-only oder config-only ist und keine der verbotenen Aktionen betrifft:
arbeite selbstständig, committe, pushe und gib am Ende einen STATUSBLOCK aus.

## Produktionspfad

Der Bot läuft auf dem Server unter /srv/housing-bot/.
Deployment erfolgt ausschließlich nach expliziter Freigabe über Docker Compose.
