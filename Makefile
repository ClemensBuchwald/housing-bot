.PHONY: install test run-mock run docker-build docker-run docker-logs docker-stop clean

# --- Lokale Entwicklung (Python) ---

install:
	python -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install pydantic pyyaml httpx python-dotenv pytest pytest-mock

test:
	PYTHONPATH=. .venv/bin/pytest tests/ -v

run-mock:
	PYTHONPATH=. .venv/bin/python -m src.main --once --mock

run:
	PYTHONPATH=. .venv/bin/python -m src.main

# --- Docker ---

docker-build:
	docker compose build

docker-run:
	docker compose up -d
	@echo "Bot läuft. Logs: make docker-logs"

docker-mock:
	docker compose run --rm bot python -m src.main --once --mock

docker-logs:
	docker compose logs -f bot

docker-stop:
	docker compose down

# --- Sonstiges ---

clean:
	rm -rf .venv data/ __pycache__ src/__pycache__ tests/__pycache__ .pytest_cache
