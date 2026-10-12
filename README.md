# agentic-ai-chatbot

An agentic chatbot with RAG and web search tools, human-in-the-loop approvals, PII detection and masking, and short-term plus long-term memory.

See the phased build plan in [docs/README.md](docs/README.md). Phase 1 (foundation) is implemented.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Docker and [Tilt](https://docs.tilt.dev/install.html).

```bash
cp .env.example .env        # add OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway)
uv sync --all-groups
tilt up                     # Postgres, Langfuse, API and chat UI, with live reload
```

Tilt's dashboard is at http://localhost:10350. From there:

| Service | URL |
|---------|-----|
| Chat UI (Streamlit) | http://localhost:8501 |
| API | http://localhost:8000 (OpenAPI docs at `/docs`) |
| Langfuse (tracing) | http://localhost:3000, login `dev@local.test` / `password` |
| Postgres + pgvector | `localhost:5433` (used from Phase 2) |

Postgres is mapped to host port 5433 so it doesn't clash with a local Postgres on 5432. Set `POSTGRES_PORT` to change it.

Without Tilt, run each piece in its own terminal: `make up`, `make dev`, `make ui`.

### Tracing

Langfuse runs locally from `docker-compose.langfuse.yml`, which `docker-compose.yml` includes. On first start it creates a project with fixed API keys, and `.env.example` already contains them, so every chat request is traced with no setup. The first `tilt up` pulls about 2 GB of images, and Langfuse takes a minute or two to become healthy.

The credentials in that file are local-only placeholders. To trace somewhere else, such as Langfuse Cloud, change the `LANGFUSE_*` values in `.env`.

```bash
curl -N localhost:8000/chat/stream \
  -H 'content-type: application/json' \
  -d '{"message": "hi"}'
```

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /chat` | Full reply as JSON |
| `POST /chat/stream` | Server-Sent Events: `token`, then `done` (or `error`) |
| `GET /healthz` | Liveness |

## Configuration

Set in `.env` (see `.env.example`):

| Variable | Default | Notes |
|----------|---------|-------|
| `OPENAI_API_KEY` | unset | Required |
| `OPENAI_BASE_URL` | unset (api.openai.com) | Set to use an OpenAI-compatible gateway |
| `MODEL` | `gpt-5-mini` | Any chat model the endpoint serves. Use `gpt-5.4` for harder questions (slower) |
| `REASONING_EFFORT` | `minimal` | `none` to `high`. Reasoning models only (gpt-5.x, o-series); remove for gpt-4.x |
| `MAX_TOKENS` | unset | Optional cap on output tokens |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | local dev keys | Tracing turns on when both are set |
| `LANGFUSE_HOST` | `http://localhost:3000` | |

## Development

```bash
make check    # ruff lint + format check, mypy, pytest
make fmt      # auto-fix lint and formatting
```
