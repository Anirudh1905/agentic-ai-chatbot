# Phase 1: Foundation

**Goal:** A working chat API backed by a minimal LangGraph agent. No tools or memory yet. The point of this phase is to get the skeleton that every later phase plugs into.

[← Overview](README.md) · [Next: Phase 2 →](phase-2-tools-rag-web-search.md)

## Scope

**In scope**
- Project setup: `uv`, `pyproject.toml`, lint/format (ruff), type checks (mypy or pyright), pytest
- Typed config loaded from env vars (`pydantic-settings`)
- A LangGraph `StateGraph` with a single LLM node
- FastAPI service with a non-streaming `/chat` and a streaming `/chat/stream` (SSE) endpoint
- Tracing for every LLM call
- Docker Compose with Postgres + pgvector (unused until Phase 2/3, but set up now)
- Streamlit chat UI that talks to `/chat/stream`
- Tiltfile that runs Postgres, the API and the UI together with live reload

**Out of scope:** tools, persistence, memory, guardrails.

## Architecture

```mermaid
flowchart LR
    Client([Streamlit UI / curl]) -->|POST /chat/stream| API[FastAPI]
    API -->|astream_events| Graph

    subgraph Graph["LangGraph StateGraph"]
        START((START)) --> LLM[llm_node]
        LLM --> END((END))
    end

    LLM <-->|Chat Completions API| OpenAI[OpenAI model]
    LLM -.trace.-> Obs[Langfuse / LangSmith]
    Config[config.py<br/>pydantic-settings] -.-> API
    Config -.-> Graph
```

## Request flow

```mermaid
sequenceDiagram
    autonumber
    participant C as Client
    participant A as FastAPI
    participant G as Graph
    participant L as OpenAI model

    C->>A: POST /chat/stream {message}
    A->>G: astream_events({messages:[HumanMessage]})
    G->>L: messages + system prompt
    L-->>G: token stream
    G-->>A: on_chat_model_stream events
    A-->>C: SSE: data: {"token": "..."}
    A-->>C: SSE: event: done
```

## Components

### State

```python
# src/chatbot/graph/state.py
from typing import Annotated, TypedDict
from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

class AgentState(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
```

Keep the state small and typed from day one. Later phases add fields (`summary`, `pii_map_id`, `user_id`), and a typed state makes those additions easy to review.

### Graph builder

```python
# src/chatbot/graph/builder.py
from langgraph.graph import StateGraph, START, END
from langchain_openai import ChatOpenAI

def build_graph(settings):
    llm = ChatOpenAI(model=settings.model, base_url=settings.openai_base_url, streaming=True)

    async def llm_node(state: AgentState):
        msgs = [SystemMessage(settings.system_prompt), *state["messages"]]
        return {"messages": [await llm.ainvoke(msgs)]}

    g = StateGraph(AgentState)
    g.add_node("llm", llm_node)
    g.add_edge(START, "llm")
    g.add_edge("llm", END)
    return g.compile()
```

Write `build_graph` as a factory that takes its dependencies (settings, and later the checkpointer, store and tools). Tests can then build the graph with fakes.

### API

| Endpoint | Purpose |
|----------|---------|
| `POST /chat` | Returns the full reply as JSON (simple, good for tests) |
| `POST /chat/stream` | SSE token stream |
| `GET /healthz` | Liveness |

Request schema: `{ "message": str, "thread_id": str | None }`. Accept `thread_id` now even though it does nothing until Phase 3, so the API contract doesn't change later.

### Config

```
OPENAI_API_KEY=
OPENAI_BASE_URL=        # optional, for an OpenAI-compatible gateway
MODEL=gpt-5-mini
REASONING_EFFORT=minimal
DATABASE_URL=postgresql://chatbot:chatbot@localhost:5433/chatbot
LANGFUSE_PUBLIC_KEY=pk-lf-local-dev
LANGFUSE_SECRET_KEY=sk-lf-local-dev
LANGFUSE_HOST=http://localhost:3000
LOG_LEVEL=INFO
```

## Implementation steps

1. `uv init`, add deps: `langgraph`, `langchain-openai`, `fastapi`, `uvicorn`, `sse-starlette`, `pydantic-settings`, `langfuse`.
2. Add `docker-compose.yml` with `pgvector/pgvector:pg16`, plus a self-hosted Langfuse stack (`docker-compose.langfuse.yml`).
3. Write `config.py`, `graph/state.py`, `graph/builder.py`.
4. Write `api/main.py` with the three endpoints; stream with `graph.astream_events(..., version="v2")` and forward `on_chat_model_stream` chunks.
5. Wire up the tracing callback handler.
6. Add a `Makefile` or `justfile` with `dev`, `test`, `lint`, `up`.

## Exit criteria

- [ ] `curl -N localhost:8000/chat/stream -d '{"message":"hi"}'` streams tokens
- [ ] Every request shows up as a trace with token counts
- [ ] Unit test runs the graph with a fake chat model (`GenericFakeChatModel`) and no network
- [ ] CI runs lint, type check and tests

## Risks and notes

- **Streaming plumbing breaks later.** Once tools and interrupts arrive, the event stream carries more than tokens. Define the SSE event types now (`token`, `tool_start`, `tool_end`, `interrupt`, `done`, `error`) even if only two are used.
- **Prompt sprawl.** Keep the system prompt in a versioned file (`prompts/system.md`), not inline strings.
