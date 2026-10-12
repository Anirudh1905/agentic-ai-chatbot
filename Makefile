.PHONY: dev ui tilt test lint fmt typecheck up down check

dev:
	uv run uvicorn chatbot.api.main:app --reload

ui:
	uv run --group ui streamlit run ui/app.py

tilt:
	tilt up

test:
	uv run pytest

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
