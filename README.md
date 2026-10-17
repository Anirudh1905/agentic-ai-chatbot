# agentic-ai-chatbot

An agentic chatbot with RAG and web search tools, human-in-the-loop approvals, PII detection and masking, and short-term plus long-term memory.

The phase plans and as-built docs are in [docs/](docs/); the [Status](#status) table below links them. The agent is LangChain's `create_agent` with middleware, using the built-ins wherever LangChain has one.

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="docs/images/architecture-dark.png">
  <source media="(prefers-color-scheme: light)" srcset="docs/images/architecture-light.png">
  <img alt="Architecture: the Streamlit UI calls the FastAPI API, which redacts PII and invokes the agent loop. The agent calls the OpenAI-compatible API, the knowledge base search (pgvector chunks, embeddings) and, through an approval gate, web search on DuckDuckGo. Threads, checkpoints, long-term memories and chunks live in Postgres with pgvector; a background MemoryWriter turns conversations into memories (at the token breach, after 30 idle minutes, or on request), a retention job prunes checkpoints, and an ingestion CLI loads the knowledge base." src="docs/images/architecture-light.png">
</picture>

## Status

| Phase | What it adds | Status |
|-------|--------------|--------|
| [1. Foundation](docs/phase-1-foundation.md) | FastAPI service, streaming chat, Streamlit UI, Langfuse tracing, logging | Built |
| [2. Tools](docs/phase-2-tools-rag-web-search.md) | Knowledge base search (hybrid vector + full-text, reranked) and DuckDuckGo web search | Built |
| [3. Short-term memory](docs/phase-3-short-term-memory-checkpointing.md) | Threads checkpointed to Postgres, resume and time travel, summarization of old turns, retention | Built |
| [4. Approvals](docs/phase-4-human-in-the-loop.md) | `HumanInTheLoopMiddleware`: risky tool calls wait for a person | Built |
| [5. PII redaction](docs/phase-5-pii-detection-masking.md) | Emails, cards, phones and IPs redacted from messages, tool results, replies and the stream | Built |
| [6. Long-term memory](docs/phase-6-long-term-memory.md) | Per-fact memories per user across threads: pushed at the token breach, after 30 idle minutes, or on request; recalled each turn, closest to the message | Built ([PR #7](https://github.com/Anirudh1905/agentic-ai-chatbot/pull/7)) |
| [7. Eval gate](docs/phase-7-eval-gate.md) | Golden sets, a judge model (DeepEval) and a regression gate on every PR | Built |
| [8. Security and observability](docs/phase-8-security-observability.md) | Login, rate limits, traces, metrics, alerts, deleting a user's data | Planned |
| [9. Reliability and deployment](docs/phase-9-reliability-deployment.md) | Retries and fallbacks, cost controls, container deployment, load test | Planned |

`make check` runs ruff, mypy and the tests (176 at the moment, unit and Postgres integration). `make eval` runs the eval suites against the real model and writes `tests/evals/report.json`, which CI checks on every PR (see [Evals](#evals)).

## Repository layout

```text
src/chatbot/
├── agent/          # builder.py (create_agent + middleware), middleware.py (prompt, call limit), approvals.py (HITL rules)
├── api/            # main.py, routes/ (chat, threads, memory, health), runs.py, streaming.py (SSE), dependencies.py
├── memory/         # short_term.py (summarization), long_term/ (user memories: facts, store, writer, middleware), threads.py, retention.py
├── tools/          # rag.py (knowledge base), web_search.py, rerank.py, memory.py (remember), registry.py
├── ingestion/      # loaders, chunking, indexing CLI
├── prompts/        # system.md, summarize.md, memory_extract.md, memory_decide.md
├── pii.py          # redaction rules, PIIMiddleware for outputs, the SSE stream redactor
├── config.py       # every setting, from .env, and RunContext (the run's user)
├── db.py, embeddings.py, observability.py
ui/app.py           # Streamlit chat UI: threads, approval card, memory panel
migrations/         # Alembic: chunks, threads (checkpoints and the memory store create their own tables)
tests/              # unit/, integration/ (need `make up`), evals/ (golden sets, judge, report)
data/sample/        # the fictional Acme Outfitters docs
docs/               # phase plans and as-built docs, images/
```

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

Postgres is mapped to host port 5433 so it doesn't clash with a local Postgres on 5432. Set `POSTGRES_PORT` to change it. pgweb browses the same database (chunks, threads, checkpoints, the memory store); `PGWEB_PORT` changes its port.

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

There's no login until Phase 8. Clients identify the user with an `X-User-Id` header, and every thread is visible only to the user who created it. The UI sends `demo-user` by default (`CHATBOT_USER_ID`, or change it in the sidebar).

Long threads stay small: once the messages pass `CONTEXT_MAX_TOKENS`, older turns are folded into a running summary by `SUMMARY_MODEL` and removed from the thread, keeping about the most recent half (and always the current turn) verbatim. This is LangChain's `SummarizationMiddleware`, adjusted to cut only at turn boundaries and to run at the end of a turn, after the reply has streamed (with a check at the start of the next turn as a fallback). Removed messages no longer appear in the chat history; the UI shows the summary in their place.

If a run stops partway (the process died during a tool call, say), `POST /threads/{id}/resume` finishes it from the last checkpoint. `GET /threads/{id}/history` lists the checkpoints, and resuming with a `checkpoint_id` from it replays the thread from that point.

Each step adds a checkpoint, so `make retention` (run it nightly) keeps only the latest one for threads idle more than `CHECKPOINT_RETENTION_DAYS`. `DELETE /threads/{id}` removes a thread and all its checkpoints.

### Approvals (human-in-the-loop)

The agent is LangChain's `create_agent` with middleware, and approvals use its `HumanInTheLoopMiddleware`. Some tool calls wait for a person before they run. A web search sends its query to an outside search engine, so it pauses when the query mentions an internal term (`HITL_SENSITIVE_TERMS`: "confidential", "internal", "Project Falcon", ...) or contains an order number. (Emails, cards and phone numbers can't reach a query: they're redacted first, see below.) Knowledge base searches never pause, and neither does `remember` (it only writes your own memories, and isn't in the approval rules). Any other tool passed to the rules always pauses, so new tools with side effects are safe by default. The rules are in `src/chatbot/agent/approvals.py`.

When a run pauses, the chat UI shows an approval card: approve, edit the query, or reject with a reason the bot sees. The thread waits in Postgres for as long as it takes, across restarts. Over the API, `/chat` returns `pending` (and `/chat/stream` sends an `interrupt` event). `POST /threads/{id}/resume` takes one decision per pending call, in the order of `pending.action_requests`:

```bash
curl localhost:8000/threads/$THREAD/resume -H 'content-type: application/json' -H 'X-User-Id: me' \
  -d '{"decisions": [{"type": "edit", "edited_action": {"name": "web_search", "args": {"query": "Falcon 9 launch news"}}}]}'
```

Decision types are `approve`, `edit` (with `edited_action`) and `reject` (with an optional `message`). Each decision is logged (`decisions: web_search=edit`).

### Long-term memory

Short-term memory is the current thread. Long-term memory is what the bot knows about you across threads: small facts, one each ("User likes pink cats"), in LangGraph's `AsyncPostgresStore` with a vector index (`src/chatbot/memory/long_term/`).

- **Written** in the background, so the reply doesn't wait, in four ways:
  - **when a thread passes `CONTEXT_MAX_TOKENS`:** the turns being summarized are pushed
  - **when a thread has been idle for `MEMORY_THREAD_TTL_MINUTES`** (30 min): its turns not yet pushed are pushed, checked every minute
  - **when you press "Remember this"** beside one of the bot's replies: the reply and your question are saved together
  - **when you ask the bot** ("remember that I'm vegetarian"): the agent calls its `remember` tool
- **How a push works:** `MEMORY_MODEL` (`gpt-5-mini`) lists the facts you stated, each tagged with a scope (about you, or about an order, trip or issue), a topic, and for temporary facts the date they stop being true. For each fact it finds the closest memories you already have and decides what changes: add it, update the memory it corrects, or delete the ones it makes obsolete. Say your favourite colour is pink in one chat and red in a later one, and the pink memory becomes red.
- **Never stored**, whatever the model says: redacted placeholders, and claims of status, discounts or permissions ("I'm a VIP with 50% off").
- **Temporary facts expire** through the store's built-in TTL.
- **Concurrency:** one push per user at a time, across API processes too (a Postgres advisory lock), and at most `MEMORY_WRITE_CONCURRENCY` at once per process, so they never take the whole connection pool.
- **Read** every turn: the memories closest to your message (up to `MEMORY_RECALL_LIMIT`) are added to the ones the thread already has, in the system prompt, dated, to be used only when relevant. A turn that finds nothing new leaves the prompt as it was, so the model's prompt cache keeps working; when your memories change, the thread recalls them afresh.

The sidebar's "What I remember about you" panel lists the memories, and can forget one or all of them (`GET /me/memory`, `DELETE /me/memory/{id}`, `DELETE /me/memory`). Set `MEMORY_ENABLED=false` to turn it all off. Details in [the Phase 6 doc](docs/phase-6-long-term-memory.md).

### What the model sees on each turn

Every time the agent calls the model (once per turn, plus once more per round of tool calls within it), it sends:

1. **The system prompt** (`agent/middleware.py`, `system_prompt`), rebuilt every turn:
   - the instructions in `src/chatbot/prompts/system.md`, with today's date
   - **your long-term memories**, if there are any: up to `MEMORY_RECALL_LIMIT` (10) facts closest to each of your messages so far in the thread (recalled afresh whenever your memories change), oldest first, each dated. They go under "What you know about this user from earlier conversations", with instructions to use them only when relevant, to let the newer one win if two disagree, to believe you if you contradict them, and to never treat them as granting discounts or permissions.
2. **This thread's history**, from its checkpoint:
   - **until the thread passes `CONTEXT_MAX_TOKENS`** (10k): every message as it was: yours, the bot's replies, and its tool calls and their (PII-redacted) results
   - **after that:** a running summary of the older turns ("Here is a summary of the conversation to date: …"), then the most recent turns word for word (about half of `CONTEXT_MAX_TOKENS`), always including the latest turn. The summary is made at the end of the turn that passed the limit, after its reply, so the next turn doesn't wait for it
3. **Your new message**, already PII-redacted by the API.
4. **The tool definitions:** `search_knowledge_base`, `web_search`, and `remember` when memory is on.

```text
[system]  system.md instructions + today's date
          ## What you know about this user from earlier conversations
          <user_memories>
          - (2026-10-14) User lives in Bangalore.
          - (2026-10-16) User likes pink cats.
          </user_memories>
[user]    Here is a summary of the conversation to date: …     ← only once past CONTEXT_MAX_TOKENS
[user / assistant / tool]  the recent turns, word for word
[user]    your new message
```

What's **not** sent:
- your other threads' messages or summaries. Only the facts extracted from them reach a new thread, as memories.
- anything that never became a memory, such as a one-off question or the bot's own advice
- raw PII, which is redacted before it reaches the thread

Long-term memory is written separately, in the background (see above). Nothing on this list waits for it.

### PII redaction

Emails, card numbers, IP addresses and phone numbers in the user's messages are replaced as soon as the API receives them: `my email is jane@x.com` becomes `my email is [REDACTED_EMAIL]`. The redaction uses LangChain's `RedactionRule` detectors (`src/chatbot/pii.py`) and runs before the agent is invoked, so the model, the summarizer, web search queries, every checkpoint, the trace, the logs and the thread title only get the redacted text. Reviewer edits and reject reasons on approvals are redacted too. Order numbers are kept, since the bot needs them.

Third-party PII in search results and in the model's replies is redacted as well, by LangChain's `PIIMiddleware` inside the agent (tool results before the model reads them, the stored reply after) and by a small redactor on the SSE stream. Emails at `PII_ALLOWED_EMAIL_DOMAINS` (Acme's own) are kept, so the bot can still give out the support address.

Redaction is one-way: the bot can't repeat the user's email back. The user's message isn't redacted with `PIIMiddleware` because LangGraph checkpoints the raw input before any middleware runs, and the stream isn't redacted with its built-in transformer because that needs LangGraph's experimental v3 streaming, which currently drops tool calls. Set `PII_ENABLED=false` to turn it off. Details in [the Phase 5 doc](docs/phase-5-pii-detection-masking.md).

### Logs

The API logs one line per request, plus what each turn did: context size and summarizing, each model call (time, tokens, tool calls), and each search (query, hit count, time). Every line carries the request ID and thread, so one conversation can be followed with `grep thread=<first 8 chars>`. Responses return the ID in an `X-Request-Id` header; send your own to correlate with a client.

```text
INFO    chatbot.api.runs         [req=59a1dee8 thread=598b8934]: redacted PII from the message: credit_card, email
INFO    chatbot.api.routes.chat  [req=59a1dee8 thread=598b8934]: new thread, message of 120 chars
INFO    chatbot.memory.short_term [req=59a1dee8 thread=598b8934]: context: 1 messages, ~39 tokens
INFO    chatbot.agent.middleware [req=59a1dee8 thread=598b8934]: model replied in 3.0s (855 tokens in, 27 out): tool calls: search_knowledge_base
INFO    chatbot.tools.rag        [req=59a1dee8 thread=598b8934]: knowledge base search 'refund timing': 6 hits in 2.2s (top: Refund Policy › Refund Timing)
INFO    chatbot.pii              [req=59a1dee8 thread=598b8934]: redacted PII from a tool result: email
INFO    chatbot.pii              [req=59a1dee8 thread=598b8934]: redacted PII from the reply: phone
INFO    chatbot.api              [req=59a1dee8 thread=598b8934]: POST /chat/stream -> 200 in 6.94s (user=me)
INFO    chatbot.memory.long_term.writer [req=91be2e70 thread=def8932c]: pushed 4 messages to long-term memory: 1 add, 2 delete, 2 update
```

What's logged:

| Area | Lines |
|------|-------|
| API | every request (method, path, status, time, user); new or continuing thread; ownership denials (warning) |
| Agent | context size and summarizing, every model call (time, tokens, tool calls or answer length), placeholder results for interrupted tool calls, a summarizer failure, and a warning when `MAX_MODEL_CALLS` stops a run |
| Tools | every search (query, hits, time, top result), web search backend failures, a search that failed after retrying, reranker load and fallback |
| Approvals | paused for approval (tool and args), the decisions, resume and delete |
| PII | each redaction, by type only, from the message, a tool result or the reply |
| Memory | memories recalled (at a thread's start, or after they change), what each push changed (`2 add, 1 dropped (entitlement claim), 1 update`), a remember request, a failed push or fact, the user deleting a memory or all of them. Never the memories themselves |

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
| `POST /threads/{id}/messages/{index}/remember` | Push one of the bot's replies, with the question before it, to your long-term memory (the "Remember this" button); 202 |
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
| `REASONING_EFFORT` | `low` | `none` to `high`. Reasoning models only (gpt-5.x, o-series). It can't be unset from `.env` (a missing or empty value means `low`), so for gpt-4.x change the default in `config.py`. `minimal` is a little faster but often skips the search for short questions |
| `MAX_TOKENS` | unset | Optional cap on output tokens |
| `EMBEDDING_MODEL` | `text-embedding-3-small` | Must produce 1536-dim vectors, or add a migration and re-ingest |
| `WEB_SEARCH_ENABLED` | `true` | DuckDuckGo web search; no key needed |
| `RETRIEVAL_TOP_K` | `6` | Chunks returned per knowledge base search |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3-int8` | Local cross-encoder that reorders the top `RERANK_CANDIDATES` (8) results. Empty turns it off |
| `MAX_MODEL_CALLS` | `6` | Model calls per request (`ModelCallLimitMiddleware`); each can be followed by tool calls |
| `SUMMARY_MODEL` | `gpt-4.1-mini` | Summarizes older turns of long threads |
| `CONTEXT_MAX_TOKENS` | `10000` | Summarize older turns once a thread's messages pass this (estimated) |
| `CHECKPOINT_RETENTION_DAYS` | `30` | `make retention` prunes checkpoint history of threads idle this long |
| `MEMORY_ENABLED` | `true` | Long-term memory: pushes, recall, the "Remember this" API and the `remember` tool |
| `MEMORY_THREAD_TTL_MINUTES` | `30` | A thread idle this long has its turns not yet pushed sent to long-term memory |
| `MEMORY_TTL_CHECK_EVERY_MINUTES` | `1` | How often to look for threads whose TTL ran out |
| `MEMORY_MODEL` | `gpt-5-mini` | Lists the facts in a thread's turns and decides what each one changes |
| `MEMORY_RECALL_LIMIT` | `10` | Long-term memories recalled into a thread |
| `REQUEST_TIMEOUT` | `30` | Seconds per model call (a reply, a summary, a memory push); a call that times out is retried twice |
| `MEMORY_WRITE_CONCURRENCY` | `2` | Pushes running at once per API process (each holds a pooled connection) |
| `PII_ENABLED` | `true` | Redact emails, cards, IPs and phone numbers from user messages, tool results and replies |
| `PII_ALLOWED_EMAIL_DOMAINS` | `["acme-outfitters.example"]` | JSON list. Emails at these domains are never redacted |
| `HITL_ENABLED` | `true` | Pause risky tool calls for approval |
| `HITL_SENSITIVE_TERMS` | see `config.py` | JSON list. Web searches mentioning these wait for approval |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | local dev keys | Tracing turns on when both are set |
| `LANGFUSE_HOST` | `http://localhost:3000` | |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds HTTP client and access logs |

## Evals

Every PR is checked for whether it made the bot worse. The model endpoint is an internal gateway that GitHub's runners can't reach, so the gate is split:

- **`make eval`**, on a machine that can reach the gateway, runs every suite and writes `tests/evals/report.json`, which is committed with the PR. It uses every setting at its default, whatever `.env` says.
- **CI** (`make eval-check`, no model calls) runs the code-only suites again, fails if `report.json` was made for different code, prompts, knowledge base or evals than the PR's, and compares it with the report on `main`.

| Suite | Scored by | Latest |
| --- | --- | --- |
| Retrieval: the right section in the top 6 | code, 28 labelled questions | 100% |
| Routing: exactly the right tools | DeepEval `ToolCorrectnessMetric` (code), 44 questions | 100% |
| Faithfulness: every claim supported by that turn's search results | DeepEval `FaithfulnessMetric`, judge `gpt-4.1` | 72% (the judge is strict; see the Phase 7 doc) |
| Citations: what each citation covers is in the result it names | DeepEval `FaithfulnessMetric` per citation | 72% |
| Correctness: agrees with a reference answer | DeepEval `GEval`, 30 knowledge base questions | 100% |
| PII: labelled values redacted, look-alikes left alone | code (`redact_pii`) | 95% recall, 92% clean (misses IPv6; reads "1.2.3.4" as an IP) |
| Approval policy: the right web searches wait for approval | code (`sensitive_reason`) | 100% |

A metric blocks a PR when it drops below `main` by more than 2 points or one case (any drop, for the code-only suites); a case that regressed is run twice more and fails on a majority. A judged metric only gates once the judge agrees with labelled answers at least 90% of the time (`make eval-calibrate`: citations 98%, faithfulness 91%, correctness 90%). The labels and reference answers were written by Claude, not a person. Details in [the Phase 7 doc](docs/phase-7-eval-gate.md).

## Development

```bash
make check    # ruff lint + format check, mypy, pytest (integration tests need `make up`)
make fmt      # auto-fix lint and formatting
make eval     # every eval suite against the real model; writes tests/evals/report.json
make eval-calibrate  # how often the judge agrees with labelled answers
make eval-check      # what CI runs: code-only suites, report.json against main's
make retention  # prune old checkpoints of idle threads
```
