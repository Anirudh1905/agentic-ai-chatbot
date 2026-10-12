.PHONY: dev ui tilt test test-unit lint fmt typecheck up down check migrate ingest eval

dev:
	uv run uvicorn chatbot.api.main:app --reload

ui:
	uv run --group ui streamlit run ui/app.py

tilt:
	tilt up

test:
	uv run pytest

test-unit:
	uv run pytest tests/unit

migrate:
	uv run alembic upgrade head

# make ingest PATH_TO_DOCS=path/to/docs
PATH_TO_DOCS ?= data/sample
ingest:
	uv run python -m chatbot.ingestion $(PATH_TO_DOCS) --prune

eval:
	uv run python tests/evals/run.py

lint:
	uv run ruff check .
	uv run ruff format --check .

fmt:
	uv run ruff check --fix .
	uv run ruff format .

typecheck:
	uv run mypy src

check: lint typecheck test

up:
	docker compose up -d

down:
	docker compose down
