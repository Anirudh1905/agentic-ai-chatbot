# Phase 1: Foundation

**Goal:** A working chat API backed by a minimal LangGraph agent. No tools or memory yet. The point of this phase is to get the skeleton that every later phase plugs into.

[← Overview](../README.md) · [Next: Phase 2 →](phase-2-tools-rag-web-search.md)

**Status: built.** Tools, persistence, memory and guardrails come in later phases.

| Area | What's built | Section |
| --- | --- | --- |
| Agent | LangChain's `create_agent` (a LangGraph graph) with no tools, so just a model call with a system prompt | [Agent](#agent) |
| API | FastAPI with `POST /chat` (JSON) and `POST /chat/stream` (SSE), and `GET /healthz` | [API and streaming](#api-and-streaming) |
| Config | Typed settings from env vars (`pydantic-settings`) | [Config and model choice](#config-and-model-choice) |
| Observability | A Langfuse trace for every model call; a request ID and thread on every log line | [Tracing and logging](#tracing-and-logging) |
| Tooling | `uv`, ruff, mypy, pytest, CI; Docker Compose with Postgres + pgvector (unused until Phase 2); a Streamlit UI; a Tiltfile | [Local stack and tooling](#local-stack-and-tooling) |

```mermaid
flowchart LR
    Client([Streamlit UI / curl]) -->|POST /chat/stream| API[FastAPI]
    API -->|astream_events| Agent

    subgraph Agent["create_agent graph (no tools yet)"]
        START((START)) --> Model["model<br/>system_prompt middleware<br/>adds the prompt + today's date"]
        Model --> END((END))
    end

    Model <-->|Chat Completions API| OpenAI[OpenAI model<br/>gpt-5.4]
    Model -.trace.-> Obs[Langfuse]
    Config[config.py<br/>pydantic-settings] -.-> API
    Config -.-> Agent
```

## Agent

`src/chatbot/agent/builder.py` and `agent/middleware.py`.

```python
# src/chatbot/agent/builder.py (Phase 1 subset)
def build_llm(settings: Settings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.model,
        base_url=settings.openai_base_url or None,
        reasoning_effort=settings.reasoning_effort,
        streaming=True,
        stream_usage=True,          # token counts on streamed responses, for logs and traces
    )

def build_graph(settings, tools=(), llm=None, ...):
    model = llm or build_llm(settings)
    return create_agent(model, tools=list(tools), middleware=[system_prompt, ...])
```

```python
# src/chatbot/agent/middleware.py (Phase 1 subset)
@dynamic_prompt
def system_prompt(_request: ModelRequest) -> str:
    return load_prompt("system").replace("{today}", date.today().isoformat())
```

- `build_graph` takes its dependencies as arguments (settings, and in later phases tools, a checkpointer and approval rules), so tests build it with a fake chat model and no network.
- The state is `create_agent`'s `AgentState`: the message list, plus internal keys its middleware uses.
- The system prompt is a versioned file, `src/chatbot/prompts/system.md`, not inline strings, so it doesn't sprawl through the code.
- **Built differently from the first plan.** Phase 1 started as a hand-written `StateGraph` with one `llm_node`. Since Phase 4 the agent is `create_agent`, and the extras later phases need (system prompt, summarization, approvals, limits) are middleware around the model call.

## API and streaming

`src/chatbot/api/routes/chat.py`, `routes/health.py` and `api/streaming.py`.

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant G as Agent
    participant L as OpenAI model

    C->>A: POST /chat/stream {message}
    A->>G: astream_events({messages:[HumanMessage]})
    G->>L: system prompt + messages
    L-->>G: token stream
    G-->>A: on_chat_model_stream events (node "model")
    A-->>C: SSE: event: token {"token": "..."}
    A-->>C: SSE: event: done
```

| Endpoint | Purpose |
| --- | --- |
| `POST /chat` | Returns the full reply as JSON (simple, good for tests) |
| `POST /chat/stream` | SSE stream: `token` events, then `done` (or `error`) |
| `GET /healthz` | Liveness |

- `stream_run` runs the graph with `astream_events(..., version="v2")` and forwards `on_chat_model_stream` chunks from the `model` node only, as `token` events. Only that node's tokens are the reply, so the summarizer (Phase 3) never leaks into it.
- **The SSE event types were defined up front** (`StreamEvent` in `api/schemas.py`: `token`, `tool_start`, `tool_end`, `interrupt`, `done`, `error`). Once tools and interrupts arrived, the stream carried more than tokens, and later phases added events without changing the contract.
- Since Phase 5, tokens pass through `StreamRedactor` (`guardrails/pii.py`) first, which redacts PII and holds back the last 128 characters, so short replies arrive in one piece.

## Config and model choice

`src/chatbot/config.py` (`Settings`, from env vars or `.env`; see [Settings](#settings)).

- `MODEL` was `gpt-5-mini` until Phase 4. `gpt-5.4` routed short questions to the right tool more reliably and was faster through the gateway (1.5 s vs 2.4 s median), at about ten times the token price.
- `REASONING_EFFORT` below `low` made both models answer questions like "president of USA" from stale memory instead of searching.

## Tracing and logging

`src/chatbot/observability/tracing.py`, `observability/logs.py` and `api/middleware.py`.

- Every run gets a Langfuse callback handler (`build_callbacks`), so each model call is a trace with its token counts. Langfuse runs self-hosted (`ops/langfuse/docker-compose.yml`).
- Every log line carries the request ID and thread, and each model call logs its time and token counts. HTTP client and access logs only show with `LOG_LEVEL=DEBUG`.

## Local stack and tooling

- `tilt up` runs Postgres, the self-hosted Langfuse stack, migrations, the API and the Streamlit UI, plus pgweb (<http://localhost:8081>) for browsing the database. Without Tilt: `make up`, `make migrate`, then `make dev` and `make ui`. (Since Phase 9, `make tilt` deploys everything to a local Kubernetes cluster instead, with the UI at <http://localhost:8088>; see [Phase 9](phase-9-reliability-deployment.md#dev-workflow).)
- `make lint`, `make typecheck` and `make test` (or `make check` for all three); CI runs the same (`.github/workflows/ci.yml`).
- The Streamlit UI (`ui/app.py`) talks to `/chat/stream`.

## Reference

### Settings

| Setting | Default | Meaning |
| --- | --- | --- |
| `OPENAI_API_KEY` | (none) | Key for the model endpoint |
| `OPENAI_BASE_URL` | (none) | Optional, for an OpenAI-compatible gateway |
| `MODEL` | `gpt-5.4` | The chat model |
| `REASONING_EFFORT` | `low` | Sent to reasoning models only |
| `DATABASE_URL` | (none, required; `postgresql://chatbot:chatbot@localhost:5433/chatbot` for the local stack) | Postgres + pgvector |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | (none; `pk-lf-local-dev` / `sk-lf-local-dev` for the local stack) | Tracing |
| `LANGFUSE_HOST` | (none; `http://localhost:3000` for the local stack) | Tracing, with both keys |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds HTTP client and access logs |

### Files

| File | What it does |
| --- | --- |
| `pyproject.toml` | Dependencies: `langchain`, `langgraph`, `langchain-openai`, `fastapi`, `uvicorn`, `sse-starlette`, `pydantic-settings`, `langfuse`; ruff, mypy and pytest config |
| `src/chatbot/config.py` | `Settings` |
| `src/chatbot/agent/builder.py`, `agent/middleware.py` | `build_llm`, `build_graph`; the `system_prompt` middleware |
| `src/chatbot/prompts/system.md` | The system prompt |
| `src/chatbot/api/main.py`, `api/routes/chat.py`, `api/routes/health.py` | The app and the three endpoints |
| `src/chatbot/api/streaming.py`, `api/schemas.py` | `stream_run`; the request, response and SSE event types |
| `src/chatbot/observability/tracing.py`, `observability/logs.py`, `api/middleware.py` | Langfuse callbacks; log format with request ID and thread |
| `docker-compose.yml`, `ops/langfuse/docker-compose.yml` | `pgvector/pgvector:pg16`, pgweb; the self-hosted Langfuse stack |
| `Makefile`, `Tiltfile`, `.github/workflows/ci.yml` | `dev`, `ui`, `test`, `lint`, `typecheck`, `up`, `migrate`; the local stack; CI |
| `ui/app.py` | The Streamlit chat UI |

### Tests

- `tests/unit/test_agent.py`: the graph returns the fake model's reply, the system prompt is prepended and includes today's date, `build_llm` uses the settings.
- `tests/unit/test_api.py`: `/healthz`; `/chat` returns the reply and rejects an empty message; `/chat/stream` emits tokens then `done` (`test_chat_stream_emits_tokens_then_done`), reports errors as an `error` event, and doesn't stream the summarizer's tokens.

## Exit criteria

- [x] `/chat/stream` streams tokens: `curl -N localhost:8000/chat/stream -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{"message":"hi"}'` (`test_chat_stream_emits_tokens_then_done`). Phase 1 identified the user with an `X-User-Id` header; since Phase 8 it's a JWT from `POST /auth/login`.
- [x] Every request shows up as a trace with token counts (Langfuse; token counts also in the API log)
- [x] Unit test runs the agent with a fake chat model and no network (`tests/unit/test_agent.py`)
- [x] CI runs lint, type check and tests (`.github/workflows/ci.yml`)
