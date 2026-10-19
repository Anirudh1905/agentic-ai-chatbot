# The app image (Phase 9): one image for the API, the worker, the UI and every Job, each with
# its own command (deploy/base). Built by Tilt (and CI) for the kind cluster.
#
#   docker build -t chatbot .
#   docker run --rm chatbot python -m chatbot.migrate
#
# No secrets or settings are baked in (.dockerignore drops .env): they come from the
# environment and /run/secrets at run time (Phase 8). The reranker model isn't either: a Job
# downloads it once into a shared volume (RERANKER_CACHE_DIR).

FROM ghcr.io/astral-sh/uv:0.11 AS uv

FROM python:3.12-slim AS build
COPY --from=uv /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
# Dependencies first, in their own layer: code changes don't reinstall them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --group ui --no-install-project
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group ui --no-editable

FROM python:3.12-slim
RUN useradd --create-home --uid 10001 app
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# The UI, the sample knowledge base (the ingest Job), and what the migrate Job needs besides
# the package.
COPY ui ./ui
COPY data/sample ./data/sample
COPY alembic.ini ./
COPY migrations ./migrations
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 RERANKER_CACHE_DIR=/models
USER app
EXPOSE 8000 8501 9100
CMD ["uvicorn", "chatbot.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
