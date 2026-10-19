# Phase 1: Foundation

**Goal:** A working chat API backed by a minimal LangGraph agent. No tools or memory yet. The point of this phase is to get the skeleton that every later phase plugs into.

[← Overview](../README.md) · [Next: Phase 2 →](phase-2-tools-rag-web-search.md)

## Scope

**In scope**
- Project setup: `uv`, `pyproject.toml`, lint/format (ruff), type checks (mypy), pytest
- Typed config loaded from env vars (`pydantic-settings`)
- An agent built with LangChain's `create_agent` (a LangGraph graph). In this phase it has no tools, so it is just a model call
- FastAPI service with a non-streaming `/chat` and a streaming `/chat/stream` (SSE) endpoint
- Tracing for every LLM call
- Docker Compose with Postgres + pgvector (unused until Phase 2/3, but set up now)
- Streamlit chat UI that talks to `/chat/stream`
- Tiltfile that runs Postgres, the API and the UI together with live reload

**Out of scope:** tools, persistence, memory, guardrails.

> **Built differently from the first plan.** Phase 1 started as a hand-written `StateGraph` with one `llm_node`. Since Phase 4 the agent is `create_agent`, and the extras later phases need (system prompt, summarization, approvals, limits) are middleware around the model call. Its builder still takes its dependencies as arguments, so tests build it with fakes.

## Architecture

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

## Request flow

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

## Components

### Agent

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
# src/chatbot/agent/middleware.py
@dynamic_prompt
def system_prompt(_request: ModelRequest) -> str:
    return load_prompt("system").replace("{today}", date.today().isoformat())
```

- `build_graph` is a factory that takes its dependencies (settings, and later tools, a checkpointer and approval rules). Tests pass a fake chat model.
- The state is `create_agent`'s `AgentState`: the message list, plus internal keys its middleware uses (`agent/builder.py`).
- The system prompt lives in `src/chatbot/prompts/system.md`, not in code.

### API

| Endpoint | Purpose |
|----------|---------|
| `POST /chat` | Returns the full reply as JSON (simple, good for tests) |
| `POST /chat/stream` | SSE stream: `token` events, then `done` (or `error`) |
| `GET /healthz` | Liveness |

`stream_run` (`api/streaming.py`) forwards `on_chat_model_stream` events from the `model` node as `token` events. Since Phase 5 they pass through `StreamRedactor` first, which redacts PII and holds back the last 128 characters, so short replies arrive in one piece. The SSE event types were defined up front (`token`, `tool_start`, `tool_end`, `interrupt`, `done`, `error`), so later phases added events without changing the contract.

### Config

```dotenv
OPENAI_API_KEY=
OPENAI_BASE_URL=        # optional, for an OpenAI-compatible gateway
MODEL=gpt-5.4
REASONING_EFFORT=low
DATABASE_URL=postgresql://chatbot:chatbot@localhost:5433/chatbot
LANGFUSE_PUBLIC_KEY=pk-lf-local-dev
LANGFUSE_SECRET_KEY=sk-lf-local-dev
LANGFUSE_HOST=http://localhost:3000
LOG_LEVEL=INFO
```

`MODEL` was `gpt-5-mini` until Phase 4. `gpt-5.4` routed short questions to the right tool more reliably and was faster through the gateway (1.5 s vs 2.4 s median), at about ten times the token price. `REASONING_EFFORT` below `low` made both models answer questions like "president of USA" from stale memory instead of searching.

### Local stack

`tilt up` runs Postgres, the self-hosted Langfuse stack, migrations, the API and the Streamlit UI, plus pgweb (<http://localhost:8081>) for browsing the database. Without Tilt: `make up`, `make migrate`, then `make dev` and `make ui`.

### Logging

Every log line carries the request ID and thread (`observability/logs.py`, `api/middleware.py`), and each model call logs its time and token counts. HTTP client and access logs only show with `LOG_LEVEL=DEBUG`.

## Implementation steps

1. `uv init`, add deps: `langchain`, `langgraph`, `langchain-openai`, `fastapi`, `uvicorn`, `sse-starlette`, `pydantic-settings`, `langfuse`.
2. Add `docker-compose.yml` with `pgvector/pgvector:pg16`, plus a self-hosted Langfuse stack (`ops/langfuse/docker-compose.yml`).
3. Write `config.py`, `agent/builder.py`, `agent/middleware.py`.
4. Write the API with the three endpoints; stream with `agent.astream_events(..., version="v2")` and forward `on_chat_model_stream` chunks from the `model` node.
5. Wire up the tracing callback handler.
6. Add a `Makefile` with `dev`, `test`, `lint`, `up`, and a Tiltfile.

## Exit criteria

- [x] `curl -N localhost:8000/chat/stream -H 'X-User-Id: me' -d '{"message":"hi"}'` streams tokens (`test_chat_stream_emits_tokens_then_done`)
- [x] Every request shows up as a trace with token counts (Langfuse; token counts also in the API log)
- [x] Unit test runs the agent with a fake chat model and no network (`tests/unit/test_agent.py`)
- [x] CI runs lint, type check and tests (`.github/workflows/ci.yml`)

## Risks and notes

- **Streaming plumbing breaks later.** Once tools and interrupts arrive, the event stream carries more than tokens. The SSE event types were defined from the start, and only the `model` node's tokens are forwarded, so the summarizer (Phase 3) never leaks into the reply.
- **Prompt sprawl.** Keep the system prompt in a versioned file (`prompts/system.md`), not inline strings.
