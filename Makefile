.PHONY: up down migrate test lint dev

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
