.PHONY: dev worker ui tilt test test-unit lint fmt typecheck up down check migrate ingest eval eval-calibrate eval-check retention

dev:
	uv run uvicorn chatbot.api.main:app --reload

# Runs the memory pushes from the queue (needs SQS_QUEUE_URL; see .env.example).
worker:
	uv run python -m chatbot.worker

ui:
	uv run --group ui streamlit run ui/app.py

tilt:
	tilt up

test:
	uv run pytest

test-unit:
	uv run pytest tests/unit

migrate:
	uv run python -m chatbot.migrate

# make ingest PATH_TO_DOCS=path/to/docs
PATH_TO_DOCS ?= data/sample
ingest:
	uv run python -m chatbot.ingestion $(PATH_TO_DOCS) --prune

# Evals (tests/evals). DeepEval is layered on for these runs only, not in the lockfile: it caps
# click<8.4, which would pull the app's huggingface-hub back to 1.16.
EVAL = PYTHONPATH=tests DEEPEVAL_TELEMETRY_OPT_OUT=YES uv run --with deepeval==4.3.1 python -m

# Every suite, against the local stack and the model gateway; writes tests/evals/report.json.
eval:
	$(MAKE) ingest PATH_TO_DOCS=data/sample
	$(EVAL) evals.run

# How often the judge agrees with the labelled answers; writes tests/evals/calibration.json.
eval-calibrate:
	$(EVAL) evals.calibrate

# What CI runs: the code-only suites, and report.json against main's. No model calls.
eval-check:
	PYTHONPATH=tests uv run python -m evals.check

retention:
	uv run python -m chatbot.memory.retention

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
