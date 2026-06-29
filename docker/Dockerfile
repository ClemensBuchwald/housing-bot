FROM python:3.12-slim

WORKDIR /app

# System-Abhängigkeiten (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python-Abhängigkeiten (separate Schicht für besseres Caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    pydantic pyyaml httpx python-dotenv beautifulsoup4 lxml anthropic

# Hinweis: Playwright/Chromium ist aktuell NICHT installiert — es gibt derzeit
# keine aktive JS-Quelle, die ihn rechtfertigt (Heimstaden=IS24-Duplikat,
# GCP=0 CW-Ausbeute). browser_base.py importiert Playwright lazy: fehlt es,
# werden JS-Quellen sauber übersprungen. Bei echter JS-Quelle wieder ergänzen:
#   RUN pip install --no-cache-dir playwright && playwright install --with-deps chromium

# Quellcode
COPY src/ ./src/

# data/-Verzeichnis anlegen (wird im Container genutzt, extern gemountet)
RUN mkdir -p /app/data

# Unprivilegierter Benutzer (Browser in /opt sind world-readable)
RUN useradd -r -u 1001 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "src.main"]
