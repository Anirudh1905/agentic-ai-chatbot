# agentic-ai-chatbot

An agentic chatbot with RAG and web search tools, human-in-the-loop approvals, PII detection and masking, and short-term plus long-term memory.

See the phased build plan in [docs/README.md](docs/README.md). Phases 1 (foundation), 2 (RAG and web search tools), 3 (short-term memory and checkpointing), 4 (human-in-the-loop approvals), 5 (PII redaction) and 6 (long-term memory) are implemented. The agent is LangChain's `create_agent` with middleware, using the built-ins wherever LangChain has one.

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

The agent is LangChain's `create_agent` with middleware, and approvals use its `HumanInTheLoopMiddleware`. Some tool calls wait for a person before they run. A web search sends its query to an outside search engine, so it pauses when the query mentions an internal term (`HITL_SENSITIVE_TERMS`: "confidential", "internal", "Project Falcon", ...) or contains an order number. (Emails, cards and phone numbers can't reach a query: they're redacted first, see below.) Knowledge base searches never pause. Any other tool always pauses, so new tools with side effects are safe by default. The rules are in `src/chatbot/agent/approvals.py`.

When a run pauses, the chat UI shows an approval card: approve, edit the query, or reject with a reason the bot sees. The thread waits in Postgres for as long as it takes, across restarts. Over the API, `/chat` returns `pending` (and `/chat/stream` sends an `interrupt` event). `POST /threads/{id}/resume` takes one decision per pending call, in the order of `pending.action_requests`:

```bash
curl localhost:8000/threads/$THREAD/resume -H 'content-type: application/json' -H 'X-User-Id: me' \
  -d '{"decisions": [{"type": "edit", "edited_action": {"name": "web_search", "args": {"query": "Falcon 9 launch news"}}}]}'
```

Decision types are `approve`, `edit` (with `edited_action`) and `reject` (with an optional `message`). Each decision is logged (`decisions: web_search=edit`).

### Long-term memory

Short-term memory is the current thread. Long-term memory is what the bot knows about you across threads: small memories, one fact each, in LangGraph's `AsyncPostgresStore` with a vector index (`src/chatbot/memory/long_term.py`).

- **Written** when a thread passes `CONTEXT_MAX_TOKENS` (10k by default), in the background, so the reply doesn't wait. `MEMORY_MODEL` (`gpt-5-mini`) splits the summary Phase 3 already makes into facts, each tagged with a scope (about you, or about an order, trip or issue) and a topic. For each fact it finds the closest memories you already have and decides what changes: add it, update the memory it corrects, or delete the ones it makes obsolete. Say your favourite colour is pink in one chat and red in a later one, and the pink memory becomes red. One write per user at a time, across API processes too (a Postgres advisory lock).
- **Read** once, when a new thread starts: the memories closest to your first message (up to `MEMORY_RECALL_LIMIT`) go into the system prompt, dated, to be used only when relevant.

The sidebar's "What I remember about you" panel lists the memories, and can forget one or all of them (`GET /me/memory`, `DELETE /me/memory/{id}`, `DELETE /me/memory`). Only threads that pass `CONTEXT_MAX_TOKENS` are remembered; lower it (the local `.env` uses 1000) to see it work sooner. Set `MEMORY_ENABLED=false` to turn it off.

### PII redaction

Emails, card numbers, IP addresses and phone numbers in the user's messages are replaced as soon as the API receives them: `my email is jane@x.com` becomes `my email is [REDACTED_EMAIL]`. The redaction uses LangChain's `RedactionRule` detectors (`src/chatbot/pii.py`) and runs before the agent is invoked, so the model, the summarizer, web search queries, every checkpoint, the trace, the logs and the thread title only get the redacted text. Reviewer edits and reject reasons on approvals are redacted too. Order numbers are kept, since the bot needs them.

Third-party PII in search results and in the model's replies is redacted as well, by LangChain's `PIIMiddleware` inside the agent (tool results before the model reads them, the stored reply after) and by a small redactor on the SSE stream. Emails at `PII_ALLOWED_EMAIL_DOMAINS` (Acme's own) are kept, so the bot can still give out the support address.

Redaction is one-way: the bot can't repeat the user's email back. The user's message isn't redacted with `PIIMiddleware` because LangGraph checkpoints the raw input before any middleware runs, and the stream isn't redacted with its built-in transformer because that needs LangGraph's experimental v3 streaming, which currently drops tool calls. Set `PII_ENABLED=false` to turn it off. Details in [the Phase 5 doc](docs/phase-5-pii-detection-masking.md).

### Logs

The API logs one line per request, plus what each turn did: context size and summarizing, each model call (time, tokens, tool calls), and each search (query, hit count, time). Every line carries the request ID and thread, so one conversation can be followed with `grep thread=<first 8 chars>`. Responses return the ID in an `X-Request-Id` header; send your own to correlate with a client.

```text
INFO    chatbot.api.runs         [req=59a1dee8 thread=598b8934]: redacted PII from the message: credit_card, email
INFO    chatbot.api.routes.chat  [req=59a1dee8 thread=598b8934]: new thread, message of 120 chars
INFO    chatbot.agent.middleware [req=59a1dee8 thread=598b8934]: context: 1 messages, ~39 tokens
INFO    chatbot.agent.middleware [req=59a1dee8 thread=598b8934]: model replied in 3.0s (855 tokens in, 27 out): tool calls: search_knowledge_base
INFO    chatbot.tools.rag        [req=59a1dee8 thread=598b8934]: knowledge base search 'refund timing': 6 hits in 2.2s (top: Refund Policy › Refund Timing)
INFO    chatbot.pii              [req=59a1dee8 thread=598b8934]: redacted PII from a tool result: email
INFO    chatbot.pii              [req=59a1dee8 thread=598b8934]: redacted PII from the reply: phone
INFO    chatbot.api              [req=59a1dee8 thread=598b8934]: POST /chat/stream -> 200 in 6.94s (user=me)
```

What's logged:

| Area | Lines |
|------|-------|
| API | every request (method, path, status, time, user); new or continuing thread; ownership denials (warning) |
| Agent | context size and summarizing, every model call (time, tokens, tool calls or answer length), placeholder results for interrupted tool calls, a summarizer failure, and a warning when `MAX_MODEL_CALLS` stops a run |
| Tools | every search (query, hits, time, top result), web search backend failures, a search that failed after retrying, reranker load and fallback |
| Approvals | paused for approval (tool and args), the decisions, resume and delete |
| PII | each redaction, by type only, from the message, a tool result or the reply |
| Memory | memories recalled (or none yet) when a thread starts, what each write changed (`2 add, 1 update, 1 delete`), a failed write, the user deleting a memory or all of them. Never the memories themselves |

HTTP client, OpenAI SDK, uvicorn access and `/healthz` lines are hidden unless `LOG_LEVEL=DEBUG`. PII values are never logged: messages are logged by length, search queries are written by the model from redacted text, and redactions are logged by type.

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
| `GET /me/memory` | What the bot remembers about you across threads (your long-term memories), newest first |
| `DELETE /me/memory/{id}` | Forget one memory |
| `DELETE /me/memory` | Forget everything |
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
| `CONTEXT_MAX_TOKENS` | `10000` | Summarize older turns once a thread's messages pass this (estimated) |
| `CHECKPOINT_RETENTION_DAYS` | `30` | `make retention` prunes checkpoint history of threads idle this long |
| `MEMORY_ENABLED` | `true` | Long-term memory: updated at token breaches, recalled when a thread starts |
| `MEMORY_MODEL` | `gpt-5-mini` | Splits summaries into memories and decides what each one changes |
| `MEMORY_RECALL_LIMIT` | `10` | Memories recalled into a new thread |
| `PII_ENABLED` | `true` | Redact emails, cards, IPs and phone numbers from user messages, tool results and replies |
| `PII_ALLOWED_EMAIL_DOMAINS` | `["acme-outfitters.example"]` | JSON list. Emails at these domains are never redacted |
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
