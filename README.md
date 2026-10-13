# agentic-ai-chatbot

An agentic chatbot with RAG and web search tools, human-in-the-loop approvals, PII detection and masking, and short-term plus long-term memory.

See the phased build plan in [docs/README.md](docs/README.md). Phases 1 (foundation), 2 (RAG and web search tools), 3 (short-term memory and checkpointing) and 4 (human-in-the-loop approvals) are implemented. The agent is LangChain's `create_agent` with middleware, using the built-ins wherever LangChain has one.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/), Docker and [Tilt](https://docs.tilt.dev/install.html).

```bash
cp .env.example .env        # add OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway)
uv sync --all-groups
tilt up                     # Postgres, Langfuse, migrations, sample docs, API and chat UI
```

Tilt's dashboard is at http://localhost:10350. From there:

| Service | URL |
|---------|-----|
| Chat UI (Streamlit) | http://localhost:8501 |
| API | http://localhost:8000 (OpenAPI docs at `/docs`) |
| Langfuse (tracing) | http://localhost:3000, login `dev@local.test` / `password` |
| Postgres + pgvector | `localhost:5433` |
| pgweb (database browser) | http://localhost:8081 |

Postgres is mapped to host port 5433 so it doesn't clash with a local Postgres on 5432. Set `POSTGRES_PORT` to change it. pgweb browses the same database (chunks, threads, checkpoints); `PGWEB_PORT` changes its port.

Without Tilt: `make up`, `make migrate`, `make ingest`, then `make dev` and `make ui` in separate terminals.

Try asking "Can I return a tent I bought at 40% off?" (answered from the sample docs) or "What's the latest news about REI?" (web search).

### Knowledge base

`tilt up` ingests the fictional Acme Outfitters docs in `data/sample/`. To add your own documents (Markdown, text, HTML, PDF, Word):

```bash
make ingest PATH_TO_DOCS=path/to/docs
```

Search combines vector and full-text matches, then a local cross-encoder reranks the top 8 before the best 6 go to the model. The reranker model (~570 MB) is downloaded once by Tilt's `reranker-model` step, or on first use, and runs on CPU (about 0.5 s per search on an M3 Pro).

Ingestion is incremental: unchanged chunks are skipped, and `--prune` (on by default in `make ingest`) removes chunks for files that were deleted. The `search_knowledge_base` tool description in `src/chatbot/tools/rag.py` and the system prompt in `src/chatbot/prompts/system.md` describe the Acme docs; update both when you ingest a different corpus, since the model uses them to decide when to search.

### Web search

The `web_search` tool uses DuckDuckGo through the [`ddgs`](https://pypi.org/project/ddgs/) package, so it needs no API key. It tries DuckDuckGo first and falls back to `ddgs`'s multi-engine search if that returns nothing. If every engine fails (keyless search is rate-limited), LangChain's `ToolRetryMiddleware` retries once after 1.5 s, then tells the model the search is unavailable. Results are short snippets (about 150 characters), so answers that need detail from a page, like exact forecasts, can be thin. Set `WEB_SEARCH_ENABLED=false` to turn it off.

Behind a TLS-inspecting proxy (such as Cloudflare Gateway or Zscaler), Python uses the OS certificate store via `truststore`, so outbound HTTPS works without extra setup.

### Conversations and memory

Every conversation is a thread, saved to Postgres after each graph step by LangGraph's `AsyncPostgresSaver`. Restarting the API, closing the tab or opening the chat in another browser picks the thread up where it left off. The chat UI lists your threads in the sidebar and keeps the current one in the URL (`?thread=...`).

There's no login until Phase 7. Clients identify the user with an `X-User-Id` header, and every thread is visible only to the user who created it. The UI sends `demo-user` by default (`CHATBOT_USER_ID`, or change it in the sidebar).

Long threads stay small: once the messages pass `CONTEXT_MAX_TOKENS`, older turns are folded into a running summary by `SUMMARY_MODEL` and removed from the thread, keeping about the most recent half (and always the current turn) verbatim. This is LangChain's `SummarizationMiddleware`, adjusted to cut only at turn boundaries and once per turn. Removed messages no longer appear in the chat history; the UI shows the summary in their place.

If a run stops partway (the process died during a tool call, say), `POST /threads/{id}/resume` finishes it from the last checkpoint. `GET /threads/{id}/history` lists the checkpoints, and resuming with a `checkpoint_id` from it replays the thread from that point.

Each step adds a checkpoint, so `make retention` (run it nightly) keeps only the latest one for threads idle more than `CHECKPOINT_RETENTION_DAYS`. `DELETE /threads/{id}` removes a thread and all its checkpoints.

### Approvals (human-in-the-loop)

The agent is LangChain's `create_agent` with middleware, and approvals use its `HumanInTheLoopMiddleware`. Some tool calls wait for a person before they run. A web search sends its query to an outside search engine, so it pauses when the query mentions an internal term (`HITL_SENSITIVE_TERMS`: "confidential", "internal", "Project Falcon", ...) or contains an email address, phone number or order number. Knowledge base searches never pause. Any other tool always pauses, so new tools with side effects are safe by default. The rules are in `src/chatbot/agent/approvals.py`.

When a run pauses, the chat UI shows an approval card: approve, edit the query, or reject with a reason the bot sees. The thread waits in Postgres for as long as it takes, across restarts. Over the API, `/chat` returns `pending` (and `/chat/stream` sends an `interrupt` event). `POST /threads/{id}/resume` takes one decision per pending call, in the order of `pending.action_requests`:

```bash
curl localhost:8000/threads/$THREAD/resume -H 'content-type: application/json' -H 'X-User-Id: me' \
  -d '{"decisions": [{"type": "edit", "edited_action": {"name": "web_search", "args": {"query": "Falcon 9 launch news"}}}]}'
```

Decision types are `approve`, `edit` (with `edited_action`) and `reject` (with an optional `message`). Each decision is logged (`decisions: web_search=edit`).

### Logs

The API logs one line per request, plus what each turn did: context size and summarizing, each model call (time, tokens, tool calls), and each search (query, hit count, time). Every line carries the request ID and thread, so one conversation can be followed with `grep thread=<first 8 chars>`. Responses return the ID in an `X-Request-Id` header; send your own to correlate with a client.

```
INFO chatbot.graph.builder [req=9f4574e3 thread=46695ac5]: context: 9 messages, ~1262/1000 tokens
INFO chatbot.graph.builder [req=9f4574e3 thread=46695ac5]: over the limit: summarizing the oldest 8 of 9 messages
INFO chatbot.tools.rag [req=e7bb1e9a thread=46695ac5]: knowledge base search 'express shipping time': 6 hits in 2.6s
INFO chatbot.api [req=e7bb1e9a thread=46695ac5]: POST /chat -> 200 in 6.94s (user=log-check)
```

HTTP client, OpenAI SDK, uvicorn access and `/healthz` lines are hidden unless `LOG_LEVEL=DEBUG`. Search queries are logged as-is, so they can contain personal data until PII masking lands in Phase 5.

### Tracing

Langfuse runs locally from `docker-compose.langfuse.yml`, which `docker-compose.yml` includes. On first start it creates a project with fixed API keys, and `.env.example` already contains them, so every chat request is traced with no setup. The first `tilt up` pulls about 2 GB of images, and Langfuse takes a minute or two to become healthy.

The credentials in that file are local-only placeholders. To trace somewhere else, such as Langfuse Cloud, change the `LANGFUSE_*` values in `.env`.

```bash
curl -N localhost:8000/chat/stream \
  -H 'content-type: application/json' -H 'X-User-Id: me' \
  -d '{"message": "hi"}'
```

## Endpoints

| Endpoint | Purpose |
|----------|---------|
| `POST /chat` | Full reply, its sources and the `thread_id` as JSON, or `pending` tool calls if it paused for approval. Omit `thread_id` to start a new thread. 409 while the thread waits for approval |
| `POST /chat/stream` | Server-Sent Events: `tool_start` / `tool_end` (with sources) and `token`, then `done` with the `thread_id` (or `error`). An `interrupt` event before `done` means the run is waiting for approval |
| `POST /threads` | Create an empty thread |
| `GET /threads` | The caller's threads, most recent first |
| `GET /threads/{id}/messages` | The thread's messages (with each reply's sources), the summary, and `pending` if it's waiting for approval |
| `GET /threads/{id}/history` | Checkpoints, newest first, for debugging |
| `POST /threads/{id}/resume` | Send approval `decisions`, finish an interrupted run, or replay from `checkpoint_id` |
| `POST /threads/{id}/resume/stream` | The same, streamed like `/chat/stream` |
| `DELETE /threads/{id}` | Delete the thread and all its checkpoints |
| `GET /healthz` | Liveness |

All endpoints except `/healthz` need an `X-User-Id` header.

## Configuration

Set in `.env` (see `.env.example`):

| Variable | Default | Notes |
|----------|---------|-------|
| `OPENAI_API_KEY` | unset | Required |
| `OPENAI_BASE_URL` | unset (api.openai.com) | Set to use an OpenAI-compatible gateway |
| `MODEL` | `gpt-5.4` | Any chat model the endpoint serves. `gpt-5-mini` costs about a tenth as much per token |
| `REASONING_EFFORT` | `low` | `none` to `high`. Reasoning models only (gpt-5.x, o-series); remove for gpt-4.x. `minimal` is a little faster but often skips the search for short questions |
| `MAX_TOKENS` | unset | Optional cap on output tokens |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Must produce 1536-dim vectors, or add a migration and re-ingest |
| `WEB_SEARCH_ENABLED` | `true` | DuckDuckGo web search; no key needed |
| `RETRIEVAL_TOP_K` | `6` | Chunks returned per knowledge base search |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3-int8` | Local cross-encoder that reorders the top `RERANK_CANDIDATES` (8) results. Empty turns it off |
| `MAX_MODEL_CALLS` | `6` | Model calls per request (`ModelCallLimitMiddleware`); each can be followed by tool calls |
| `SUMMARY_MODEL` | `gpt-5-nano` | Summarizes older turns of long threads |
| `CONTEXT_MAX_TOKENS` | `32000` | Summarize older turns once a thread's messages pass this (estimated) |
| `CHECKPOINT_RETENTION_DAYS` | `30` | `make retention` prunes checkpoint history of threads idle this long |
| `HITL_ENABLED` | `true` | Pause risky tool calls for approval |
| `HITL_SENSITIVE_TERMS` | see `config.py` | JSON list. Web searches mentioning these wait for approval |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | local dev keys | Tracing turns on when both are set |
| `LANGFUSE_HOST` | `http://localhost:3000` | |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds HTTP client and access logs |

## Development

```bash
make check    # ruff lint + format check, mypy, pytest (integration tests need `make up`)
make fmt      # auto-fix lint and formatting
make eval     # retrieval recall and tool routing on tests/evals/golden.jsonl (calls the model)
make retention  # prune old checkpoints of idle threads
```
