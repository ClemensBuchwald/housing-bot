FROM python:3.12-slim

WORKDIR /app

# System-Abhängigkeiten (minimal)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Browser an festen, für alle User lesbaren Ort installieren
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright

# Python-Abhängigkeiten (separate Schicht für besseres Caching)
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    pydantic pyyaml httpx python-dotenv beautifulsoup4 lxml anthropic playwright

# Chromium + System-Dependencies für Playwright (JS-Quellen).
# Vergrößert das Image (~400 MB), ist aber reproduzierbar und wartbar.
RUN playwright install --with-deps chromium

# Quellcode
COPY src/ ./src/

# data/-Verzeichnis anlegen (wird im Container genutzt, extern gemountet)
RUN mkdir -p /app/data

# Unprivilegierter Benutzer (Browser in /opt sind world-readable)
RUN useradd -r -u 1001 botuser && chown -R botuser:botuser /app
USER botuser

CMD ["python", "-m", "src.main"]
