.PHONY: up down migrate test lint dev pool

up:
	docker compose up -d --wait

down:
	docker compose down

migrate:
	uv run python -m fern.platform.migrate

test:
	uv run pytest -q

lint:
	uv run ruff check src tests
	uv run ruff format --check src tests

dev: migrate
	uv run uvicorn fern.api:app --reload

# Pre-fund the demo account pool (DESIGN §2.1). Uses FERN_BANK; ~2-3 minutes on Column.
SIZE ?= 60
pool: migrate
	uv run python -m fern.pool --size $(SIZE)
