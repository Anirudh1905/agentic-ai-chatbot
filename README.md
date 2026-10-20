# agentic-ai-chatbot

An agentic chatbot with RAG and web search tools, human-in-the-loop approvals, PII detection and masking, short-term and long-term memory, accounts with rate limits, and traces, metrics and alerts.

The phase plans and as-built docs are in [docs/](docs/); the [Status](#status) table below links them. The agent is LangChain's `create_agent` with middleware, using the built-ins wherever LangChain has one.

<img alt="Architecture on Kubernetes. A browser reaches Envoy Gateway on localhost:8088 in a kind cluster: / goes to the Streamlit ui pod and /api to two FastAPI API pods. In each API pod a request passes the FastAPI app, then Auth + limits (JWT, rate limits and token budget in Redis, accounts in Postgres), then the agent loop, which calls the model gateway, checkpoints and recalls memories in Postgres (CloudNativePG), and uses three tools: web_search (DuckDuckGo; sensitive queries wait for approval), search_knowledge_base (Postgres chunks, filled by the ingest Job) and remember. Memory pushes go to an ElasticMQ (SQS) FIFO queue with a dead-letter queue; the worker long-polls it and writes memories to Postgres. migrate and retention Jobs maintain the schema and checkpoints. kube-prometheus-stack, behind /grafana, scrapes the API pods and the worker. The app sends run traces to Langfuse in Docker Compose, and the eval suites send their scores there." src="docs/images/architecture-dark.png">

## Status

| Phase | What it adds | Status |
| --- | --- | --- |
| [1. Foundation](docs/phase-1-foundation.md) | FastAPI service, streaming chat, Streamlit UI, Langfuse tracing, logging | Built |
| [2. Tools](docs/phase-2-tools-rag-web-search.md) | Knowledge base search (hybrid vector + full-text, reranked) and DuckDuckGo web search | Built |
| [3. Short-term memory](docs/phase-3-short-term-memory-checkpointing.md) | Threads checkpointed to Postgres, resume and time travel, summarization of old turns, retention | Built |
| [4. Approvals](docs/phase-4-human-in-the-loop.md) | `HumanInTheLoopMiddleware`: risky tool calls wait for a person | Built |
| [5. PII redaction](docs/phase-5-pii-detection-masking.md) | Emails, cards, phones and IPs redacted from messages, tool results, replies and the stream | Built |
| [6. Long-term memory](docs/phase-6-long-term-memory.md) | Per-fact memories per user across threads: pushed at the token breach, after 30 idle minutes, or on request; recalled each turn, closest to the message | Built ([PR #7](https://github.com/Anirudh1905/agentic-ai-chatbot/pull/7)) |
| [7. Eval gate](docs/phase-7-eval-gate.md) | Golden sets, a judge model (DeepEval) and a regression gate on every PR | Built ([PR #8](https://github.com/Anirudh1905/agentic-ai-chatbot/pull/8)) |
| [8. Security and observability](docs/phase-8-security-observability.md) | Accounts and login (JWT), your own model key, Redis rate limits and token budgets, deleting a user's data, trace metadata, Prometheus metrics, a Grafana dashboard and alerts | Built ([PR #9](https://github.com/Anirudh1905/agentic-ai-chatbot/pull/9)) |
| [9. Reliability and deployment](docs/phase-9-reliability-deployment.md) | Kubernetes (kind): 2 API replicas behind Envoy Gateway, memory pushes on an SQS queue run by a worker, CloudNativePG, kube-prometheus-stack, a kind smoke test in CI; retries, circuit breakers and a fallback model, a per-thread lock, Postgres backups with a tested point-in-time restore; prompt caching measured, caps on context, a web search cache; a load test | Deployment, reliability, and cost and latency built, the load test run (on `phase-9-reliability-deployment`); the outage test and runbook planned |

`make check` runs ruff, mypy and the tests (289 at the moment: unit, and integration against the cluster's Postgres, ElasticMQ and Redis). `make eval` runs the eval suites against the real model and writes `tests/evals/report.json`, which CI checks on every PR (see [Evals](#evals)).

## Repository layout

```text
src/chatbot/
├── config.py       # every setting (env, /run/secrets, .env), and RunContext (the run's user)
├── agent/          # builder.py (create_agent + middleware), middleware.py (prompt, call limit, the user's own model, metrics), approvals.py (HITL rules)
├── api/            # main.py, routes/ (auth, chat, threads, me, health), security.py (the JWT and the signed-in user), middleware.py (request log and metrics), runs.py, streaming.py (SSE), schemas.py, dependencies.py
├── accounts/       # users.py (email and argon2id hash), limits.py (rate limits and the daily token budget, Redis)
├── guardrails/     # pii.py: redaction rules, PIIMiddleware for outputs, the SSE stream redactor
├── memory/         # short_term.py (summarization), long_term/ (user memories: facts, store, writer, middleware), threads.py, retention.py
├── tools/          # rag.py (knowledge base), web_search.py, rerank.py, memory.py (remember), registry.py
├── ingestion/      # loaders, chunking, indexing CLI
├── prompts/        # system.md, summarize.md, memory_extract.md, memory_decide.md; as_data (defuses untrusted text)
├── jobs/           # push.py (a memory push as a queue message), queue.py (JobQueue: SQS, in memory)
├── observability/  # logs.py (request and thread on every line), tracing.py (Langfuse), metrics.py (Prometheus)
├── storage/        # db.py (Postgres pool), embeddings.py, langgraph.py (the checkpointer and the store)
├── migrate.py      # Alembic, then the checkpointer's and the store's tables
└── worker.py       # runs the memory pushes from the queue, the idle-thread scan and the TTL sweeper
ui/app.py           # Streamlit chat UI: login, threads, approval card, memory panel, account deletion
migrations/         # Alembic: chunks, threads, users (checkpoints and the memory store create their own tables)
deploy/             # kind.yaml, cluster.sh, helm/ (chart values), base/ (our manifests), local/ (the kind overlay), ci/, smoke.sh
ops/langfuse/       # Langfuse's compose file
Dockerfile          # the one app image: API, worker, UI and every Job
tests/              # unit/, integration/ (need the cluster running), evals/ (golden sets, judge, report)
data/sample/        # the fictional Acme Outfitters docs
docs/               # phase plans and as-built docs, images/
```

## Quickstart

The stack runs on a local Kubernetes cluster ([kind](https://kind.sigs.k8s.io/)); Langfuse runs beside it in Docker Compose. Requires [uv](https://docs.astral.sh/uv/), Docker, kind, kubectl, Helm and [Tilt](https://docs.tilt.dev/install.html).

```bash
cp .env.example .env        # add OPENAI_API_KEY (and OPENAI_BASE_URL for a gateway)
echo "JWT_SECRET=$(openssl rand -hex 32)" >> .env   # signs login tokens; without it every request gets 401
uv sync --all-groups
make cluster                # once: the kind cluster, Envoy Gateway, CloudNativePG (+ its backup plugin and
                            # cert-manager), kube-prometheus-stack
make secrets                # the cluster's Secret and trust store, from .env and this machine
make tilt                   # Langfuse, the image, Postgres (backed up to MinIO), Redis, ElasticMQ, migrations,
                            # the reranker model, sample docs, 2 API pods, the worker and the chat UI
```

Tilt's dashboard is at http://localhost:10350; it only ever deploys to the `kind-chatbot` context. Open the chat UI, create an account and log in. From there:

| Service | URL |
| --- | --- |
| Chat UI (Streamlit) | http://localhost:8088 |
| API (both pods) | http://localhost:8088/api (OpenAPI docs at `/api/docs`) |
| Grafana (the Chatbot dashboard) | http://localhost:8088/grafana/d/chatbot, read only without a login |
| Prometheus (metrics, alerts) | http://localhost:8088/prometheus/alerts |
| Alertmanager | http://localhost:8088/alertmanager |
| Langfuse (tracing) | http://localhost:3000, login `dev@local.test` / `password` |
| Postgres + pgvector (CloudNativePG) | `localhost:5433`, login `dev` / `dev` (local only) |
| pgweb (database browser) | http://localhost:8081 |
| Redis (rate limits) | `localhost:6380`, port-forwarded |
| ElasticMQ (the SQS queue) | `localhost:9324`, port-forwarded |
| MinIO (Postgres backups) | http://localhost:9001, the keys in `deploy/local/backup-store.env` |

Everything in the cluster comes in through one gateway on port 8088 (80 and 8080 are often taken). The host ports are for the tests, `make eval` and database tools, with the URLs in `.env.example`; pgweb browses the same database (chunks, threads, checkpoints, users, the memory store). A code change rebuilds the image and rolls the pods in about 20 s. Details, and what each piece is for, in [the Phase 9 doc](docs/phase-9-reliability-deployment.md#deployment).

Without Kubernetes: start Postgres and Redis yourself, then `make migrate`, `make ingest`, `make dev` and `make ui`. Without `SQS_QUEUE_URL`, memory pushes run in the API process instead of a worker.

Try asking "Can I return a tent I bought at 40% off?" (answered from the sample docs) or "What's the latest news about REI?" (web search).

<img alt="The chat UI: a question about returning a clearance jacket and packing for a trek, answered from the refund policy with citations and a Sources list, and a Remember this button beside the reply. The sidebar shows the user, their chats, and what the bot remembers about them (lives in Bangalore, a Ladakh trek in April 2027). The answer also covers the EU right of withdrawal." src="docs/images/ui-chat-dark.png" width="800">

The answer cites the refund policy, lists its sources, and uses two long-term memories from an earlier conversation (the sidebar's "What I remember about you"). The bookmark button beside the reply saves it to memory.

## How it works

### Accounts and limits

Every request needs a JWT in `Authorization: Bearer <token>`; the user is its `sub`, and every thread and memory is visible only to its owner. The API checks the token's signature (HS256, `JWT_SECRET`, which `/auth/login` signs with) and expiry, and that its account still exists. The chat UI opens on a login screen: create an account with an email and a password, then log in. Optionally give your own OpenAI-compatible base URL and API key there: the login checks them (and fails, saying so, if they don't work), and your chats then run on them instead of the server's key. They stay in your browser session, sent with each request; the server doesn't store them. To call the API yourself:

```bash
curl -s localhost:8088/api/auth/register -H 'content-type: application/json' -d '{"email": "me@example.com", "password": "a-long-password"}'
TOKEN=$(curl -s localhost:8088/api/auth/login -d 'username=me@example.com&password=a-long-password' | jq -r .access_token)
curl -N localhost:8088/api/chat/stream -H 'content-type: application/json' -H "Authorization: Bearer $TOKEN" -d '{"message": "hi"}'
```

Requests are limited per user (`RATE_LIMIT_PER_USER`, 120 a minute) and per IP (`RATE_LIMIT_PER_IP`, 300 a minute), and each user's agent runs to `DAILY_TOKEN_BUDGET` model tokens a day (500,000). Over a limit, the API answers 429 with `Retry-After`. The counts live in Redis, so every API pod shares them; if Redis is down, requests go through. `DELETE /me` (the sidebar's "Delete my account") deletes the account with every thread, checkpoint and memory. Details in [the Phase 8 doc](docs/phase-8-security-observability.md).

### Knowledge base

`make tilt` ingests the fictional Acme Outfitters docs in `data/sample/` (the `ingest` Job; re-run it from Tilt). To add your own documents (Markdown, text, HTML, PDF, Word):

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

Long threads stay small: once the messages pass `CONTEXT_MAX_TOKENS`, older turns are folded into a running summary by `SUMMARY_MODEL` and removed from the thread, keeping about the most recent half (and always the current turn) verbatim. This is LangChain's `SummarizationMiddleware`, adjusted to cut only at turn boundaries and to run at the end of a turn, after the reply has streamed (with a check at the start of the next turn as a fallback). Removed messages no longer appear in the chat history; the UI shows the summary in their place.

If a run stops partway (the process died during a tool call, say), `POST /threads/{id}/resume` finishes it from the last checkpoint. `GET /threads/{id}/history` lists the checkpoints, and resuming with a `checkpoint_id` from it replays the thread from that point.

Each step adds a checkpoint, so `make retention` (run it nightly) keeps only the latest one for threads idle more than `CHECKPOINT_RETENTION_DAYS`. `DELETE /threads/{id}` removes a thread and all its checkpoints.

### Approvals (human-in-the-loop)

The agent is LangChain's `create_agent` with middleware, and approvals use its `HumanInTheLoopMiddleware`. Some tool calls wait for a person before they run. A web search sends its query to an outside search engine, so it pauses when the query mentions an internal term (`HITL_SENSITIVE_TERMS`: "confidential", "internal", "Project Falcon", ...) or contains an order number. (Emails, cards and phone numbers can't reach a query: they're redacted first, see below.) Knowledge base searches never pause, and neither does `remember` (it only writes your own memories, and isn't in the approval rules). Any other tool passed to the rules always pauses, so new tools with side effects are safe by default. The rules are in `src/chatbot/agent/approvals.py`.

When a run pauses, the chat UI shows an approval card: approve, edit the query, or reject with a reason the bot sees. The thread waits in Postgres for as long as it takes, across restarts. Over the API, `/chat` returns `pending` (and `/chat/stream` sends an `interrupt` event). `POST /threads/{id}/resume` takes one decision per pending call, in the order of `pending.action_requests`:

```bash
curl localhost:8088/api/threads/$THREAD/resume -H 'content-type: application/json' -H "Authorization: Bearer $TOKEN" \
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
- **Through a queue:** in the cluster every push is a message on an SQS FIFO queue (ElasticMQ locally), grouped by user, and the worker runs it, so a push survives an API pod being replaced. A push that fails comes back after 5 minutes; after 5 tries it lands in a dead-letter queue, which alerts. If the queue can't be reached, the API runs the push itself.
- **Concurrency:** one push per user at a time, across processes too (the queue's user groups, and a Postgres advisory lock), and at most `MEMORY_WRITE_CONCURRENCY` at once per process, so they never take the whole connection pool.
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

Emails, card numbers, IP addresses and phone numbers in the user's messages are replaced as soon as the API receives them: `my email is jane@x.com` becomes `my email is [REDACTED_EMAIL]`. The redaction uses LangChain's `RedactionRule` detectors (`src/chatbot/guardrails/pii.py`) and runs before the agent is invoked, so the model, the summarizer, web search queries, every checkpoint, the trace, the logs and the thread title only get the redacted text. Reviewer edits and reject reasons on approvals are redacted too. Order numbers are kept, since the bot needs them.

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
INFO    chatbot.guardrails.pii   [req=59a1dee8 thread=598b8934]: redacted PII from a tool result: email
INFO    chatbot.guardrails.pii   [req=59a1dee8 thread=598b8934]: redacted PII from the reply: phone
INFO    chatbot.api              [req=59a1dee8 thread=598b8934]: POST /chat/stream -> 200 in 6.94s (user=bb1dbc66-…)
INFO    chatbot.memory.long_term.writer [req=91be2e70 thread=def8932c]: pushed 4 messages to long-term memory: 1 add, 2 delete, 2 update
```

What's logged:

| Area | Lines |
| --- | --- |
| API | every request (method, path, status, time, the signed-in user's id); new or continuing thread; ownership denials (warning) |
| Accounts and limits | registrations and logins by account id, a failed login by client address (never the email), why a token was refused, a rate limit or spent budget, Redis being unreachable (warning), an account deleted |
| Agent | context size and summarizing, every model call (time, tokens, tool calls or answer length), placeholder results for interrupted tool calls, a summarizer failure, and a warning when `MAX_MODEL_CALLS` stops a run |
| Tools | every search (query, hits, time, top result), web search backend failures, a search that failed after retrying, reranker load and fallback |
| Approvals | paused for approval (tool and args), the decisions, resume and delete |
| PII | each redaction, by type only, from the message, a tool result or the reply |
| Memory | memories recalled (every turn that finds new ones), what each push changed (`2 add, 1 dropped (entitlement claim), 1 update`), a remember request, a failed push or fact, the user deleting a memory or all of them. Never the memories themselves |

HTTP client, OpenAI SDK, uvicorn access, `/healthz` and `/metrics` lines are hidden unless `LOG_LEVEL=DEBUG`. PII values are never logged: messages are logged by length, search queries are written by the model from redacted text, and redactions are logged by type.

### Tracing

Langfuse runs locally from `ops/langfuse/docker-compose.yml` (`make langfuse`, or Tilt). On first start it creates a project with fixed API keys, and `.env.example` already contains them, so every chat request is traced with no setup. The first `make tilt` pulls about 2 GB of images, and Langfuse takes a minute or two to become healthy. Pods reach it as `http://langfuse-web:3000`, through a Service Tilt points at the container (`deploy/local/langfuse-link.sh`).

The credentials in that file are local-only placeholders. To trace somewhere else, such as Langfuse Cloud, change the `LANGFUSE_*` values in `.env`.

Each run's trace is named `chat` or `resume`, grouped by thread (Langfuse's session), with the user as a hash (`hashed_user`) and tags (`own-key` when the user brought their own model). `make eval` traces its runs too, tagged `eval`, with a score per metric on each, so a failed case links to its run.

### Metrics and alerts

The API serves Prometheus metrics: requests and latency by route, time to first token, open streams, model latency, tokens and errors by model, tool calls by outcome, approval pauses and decisions, PII redactions by type, memory recall hits, prompt cache and web search cache hits, refusals by limit (429s, and 409s for a busy thread), the memory queue's depth and pushes, and open circuit breakers. In the cluster they're on a separate port (`METRICS_PORT`) that the gateway doesn't route, and Prometheus scrapes every API pod, the worker and Postgres; outside it, `GET /metrics`. Grafana charts them on the "Chatbot" dashboard, and thirteen alerts (API or worker down, error rate, slow first token, slow or failing model, token spike, PII in replies, pushes dead-lettered or stuck, a circuit breaker open, no Postgres backup in a day, WAL archiving failing) are a `PrometheusRule` in `deploy/base/monitoring.yaml`. They show as firing in Prometheus, on the dashboard and in Alertmanager; nothing pages. Details in [the Phase 8 doc](docs/phase-8-security-observability.md#observability) and [Phase 9](docs/phase-9-reliability-deployment.md#deployment).

The "Chatbot" dashboard under `make load` with 5 users on the kind cluster: no errors, p95 time to first token 13.7 to 18.5 s across runs, held back by the CPU reranker. Details in [Load test](docs/phase-9-reliability-deployment.md#load-test):

<img alt="The Chatbot dashboard in Grafana, dark theme, during a 5-user load test: about 1 request a second, 0% errors, p95 time to first token 14.8 s, request and model call latency by route and model, tokens per minute rising, and the ChatbotTokenSpike alert firing." src="docs/images/grafana-dark.png" width="800">

Model calls that time out or fail are retried once, then go to the fallback model (`gpt-4.1`); circuit breakers, shared in Redis, skip a model, the embeddings or web search while they're down. Postgres archives its WAL to MinIO continuously and takes a base backup nightly; `make restore TARGET="…"` restores to a point in time into a second cluster, and `make restore-test` proves it. Details in [Reliability](docs/phase-9-reliability-deployment.md#reliability).

For cost: the gateway caches prompt prefixes on its own, and the dashboard shows the hit rate; knowledge base results are capped at `RETRIEVAL_MAX_TOKENS`, a long thread's older search results are left out of what the model is sent, and web search results are cached in Redis for 10 minutes. Details in [Cost and latency](docs/phase-9-reliability-deployment.md#cost-and-latency).

## Endpoints

| Endpoint | Purpose |
| --- | --- |
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
| `DELETE /me` | Delete your account and all your data: threads, checkpoints, memories (the "Delete my account" button); 204 |
| `POST /auth/register` | Create an account (`{email, password}`); 409 if the email is taken |
| `POST /auth/login` | Log in (form `username` = email, `password`, optionally `openai_api_key` / `openai_base_url`, checked); returns a bearer token |
| `GET /auth/me` | The signed-in account |
| `GET /healthz` | Liveness |
| `GET /readyz` | Readiness: 503 when the database doesn't answer |
| `GET /metrics` | Prometheus metrics (404 when `METRICS_PORT` moves them to their own port, as in the cluster) |

All endpoints except `/healthz`, `/readyz`, `/metrics`, `/auth/register` and `/auth/login` need a bearer token (401 without a valid one), and answer 429 with `Retry-After` over a rate limit. The ones that start a model run (`/chat`, `/chat/stream`, `.../resume`, `.../resume/stream`) also answer 429 once the user's daily token budget is spent.

## Configuration

Set in `.env` (see `.env.example`), the environment, or, for secrets, files in `/run/secrets` named like the setting (`jwt_secret`, `openai_api_key`), as Docker and Kubernetes mount them:

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | unset | Required |
| `OPENAI_BASE_URL` | unset (api.openai.com) | Set to use an OpenAI-compatible gateway |
| `MODEL` | `gpt-5.4` | Any chat model the endpoint serves. `gpt-5-mini` costs about a tenth as much per token |
| `REASONING_EFFORT` | `low` | `none` to `high`. Reasoning models only (gpt-5.x, o-series). It can't be unset from `.env` (a missing or empty value means `low`), so for gpt-4.x change the default in `config.py`. `minimal` is a little faster but often skips the search for short questions |
| `MAX_TOKENS` | unset | Optional cap on output tokens |
| `DATABASE_URL` | unset, required | Postgres; `.env.example` has the kind cluster's, as the local `dev` login (`postgresql://dev:dev@localhost:5433/chatbot`) |
| `EMBEDDING_MODEL` / `EMBEDDING_DIMENSIONS` | `text-embedding-3-small` / `1536` | The dimensions must match the vector column (`0001_chunks`); changing them needs a migration and a re-ingest |
| `CHUNK_SIZE_TOKENS` / `CHUNK_OVERLAP_TOKENS` | `600` / `80` | How documents are split at ingestion |
| `WEB_SEARCH_ENABLED` | `true` | DuckDuckGo web search; no key needed |
| `WEB_SEARCH_MAX_RESULTS` / `WEB_SEARCH_TIMEOUT` | `5` / `15` | Results per search, and seconds before a search gives up |
| `WEB_SEARCH_CACHE_SECONDS` | `600` | How long a query's results are kept in Redis, shared by the API pods. `0`, or no `REDIS_URL`: no cache |
| `RETRIEVAL_CANDIDATES` / `RETRIEVAL_TOP_K` | `30` / `6` | Candidates from each of vector and full-text search before fusion, and chunks returned per knowledge base search |
| `RETRIEVAL_MAX_TOKENS` | `2500` | Cap on one knowledge base search's results (estimated tokens): the best-ranked chunks that fit, at least one |
| `RERANKER_MODEL` | `BAAI/bge-reranker-v2-m3-int8` | Local cross-encoder that reorders the top `RERANK_CANDIDATES` (8) results. Empty turns it off |
| `RERANKER_CACHE_DIR` | `~/.cache/fastembed` | Where the reranker model is downloaded |
| `MAX_MODEL_CALLS` | `6` | Model calls per request (`ModelCallLimitMiddleware`); each can be followed by tool calls |
| `SUMMARY_MODEL` | `gpt-4.1-mini` | Summarizes older turns of long threads |
| `CONTEXT_MAX_TOKENS` | `10000` | Summarize older turns once a thread's messages pass this (estimated) |
| `CONTEXT_CLEAR_TOOL_RESULTS_TOKENS` / `CONTEXT_KEEP_TOOL_RESULTS` | `6000` / `3` | Past this many tokens, the model is sent only the latest few tool results (`ContextEditingMiddleware`); the thread keeps them all. `0`: off |
| `CHECKPOINT_RETENTION_DAYS` | `30` | `make retention` prunes checkpoint history of threads idle this long |
| `MEMORY_ENABLED` | `true` | Long-term memory: pushes, recall, the "Remember this" API and the `remember` tool |
| `MEMORY_THREAD_TTL_MINUTES` | `30` | A thread idle this long has its turns not yet pushed sent to long-term memory |
| `MEMORY_TTL_CHECK_EVERY_MINUTES` | `1` | How often to look for threads whose TTL ran out |
| `MEMORY_MODEL` | `gpt-5-mini` | Lists the facts in a thread's turns and decides what each one changes |
| `MEMORY_RECALL_LIMIT` | `10` | Long-term memories recalled into a thread |
| `REQUEST_TIMEOUT` | `30` | Seconds per model call (a reply, a summary, a memory push) |
| `MODEL_MAX_RETRIES` | `1` | Retries of a model call that timed out, couldn't connect, or got a 429 or 5xx (`ModelRetryMiddleware`) |
| `FALLBACK_MODEL` | `gpt-4.1` | Answers on the same endpoint when `MODEL` fails (`ModelFallbackMiddleware`); memory pushes fall back to it too. Empty: no fallback |
| `BREAKER_FAILURES` / `BREAKER_WINDOW_SECONDS` / `BREAKER_COOLDOWN_SECONDS` | `5` / `60` / `30` | Circuit breakers per model, the embeddings and web search, shared in Redis: this many outages within the window open one for the cooldown |
| `THREAD_LEASE_SECONDS` | `60` | A run's lock on its thread, renewed every third of it; a second message meanwhile gets 409. A pod that dies frees its threads this long after |
| `MEMORY_WRITE_CONCURRENCY` | `2` | Pushes running at once per process (each holds a pooled connection) |
| `SQS_QUEUE_URL` / `SQS_DEAD_LETTER_QUEUE_URL` | unset | The memory-push queue and its dead-letter queue. Unset: pushes run in the API process |
| `SQS_ENDPOINT_URL` | unset (AWS) | ElasticMQ's address locally (`http://localhost:9324`, or `http://elasticmq:9324` in the cluster) |
| `SQS_REGION` / `SQS_ACCESS_KEY_ID` / `SQS_SECRET_ACCESS_KEY` | `us-east-1` / unset / unset | Unset keys use AWS's usual credential chain; ElasticMQ takes any |
| `BACKGROUND_JOBS` | `true` | Whether this process runs the idle-thread scan and the store's TTL sweeper; `false` in the API pods, where the worker runs them |
| `METRICS_PORT` | unset | Serve the metrics on this port instead of `GET /metrics` (9100 in the cluster) |
| `WORKER_METRICS_PORT` | `9100` | The worker's metrics port |
| `PII_ENABLED` | `true` | Redact emails, cards, IPs and phone numbers from user messages, tool results and replies |
| `PII_ALLOWED_EMAIL_DOMAINS` | `["acme-outfitters.example"]` | JSON list. Emails at these domains are never redacted |
| `HITL_ENABLED` | `true` | Pause risky tool calls for approval |
| `HITL_SENSITIVE_TERMS` | see `config.py` | JSON list. Web searches mentioning these wait for approval |
| `JWT_SECRET` | unset | What `/auth/login` signs tokens with (HS256) and the API verifies them with. At least 32 characters (`openssl rand -hex 32`); a shorter one stops the app from starting, and without one every request gets 401 |
| `JWT_TTL_MINUTES` | `60` | How long a login lasts |
| `RATE_LIMITS_ENABLED` | `true` | Rate limits and the token budget |
| `REDIS_URL` | unset, required with rate limits on | Where the limits are counted; `.env.example` has the kind cluster's, through Tilt (`redis://localhost:6380/0`) |
| `RATE_LIMIT_PER_USER` / `RATE_LIMIT_PER_IP` | `120/minute` / `300/minute` | Requests allowed, in the `limits` library's notation |
| `RATE_LIMIT_LOGIN` | `10/minute` | Register and log-in attempts per IP |
| `DAILY_TOKEN_BUDGET` | `500000` | Model tokens per user per UTC day for agent runs; 0 turns it off |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | local dev keys | Tracing turns on when both are set |
| `LANGFUSE_HOST` | unset | Tracing needs it with both keys; `.env.example` has the local Langfuse (`http://localhost:3000`) |
| `JUDGE_MODEL` | `gpt-4.1` | Scores answers in the evals; changing it means `make eval-calibrate` |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds HTTP client and access logs |

## Evals

Every PR is checked for whether it made the bot worse. The model endpoint is an internal gateway that GitHub's runners can't reach, so the gate is split:

- **`make eval`**, on a machine that can reach the gateway, runs every suite and writes `tests/evals/report.json`, which is committed with the PR. It uses every setting at its default, whatever `.env` says.
- **CI** (`make eval-check`, no model calls) runs the code-only suites again, fails if `report.json` was made for different code, prompts, knowledge base or evals than the PR's, and compares it with the report on `main`.

| Suite | Scored by | On `main` |
| --- | --- | --- |
| Retrieval: the right section in the top 6 | code, 28 labelled questions | 100% |
| Routing: exactly the right tools | DeepEval `ToolCorrectnessMetric` (code), 44 questions | 100% |
| Faithfulness: every claim supported by that turn's search results | DeepEval `FaithfulnessMetric`, judge `gpt-4.1` | 72% (the judge is strict; see the Phase 7 doc) |
| Citations: what each citation covers is in the result it names | DeepEval `FaithfulnessMetric` per citation | 72% |
| Correctness: agrees with a reference answer | DeepEval `GEval`, 30 knowledge base questions | 100% |
| PII: labelled values redacted, look-alikes left alone | code (`redact_pii`) | 95% recall, 92% clean (misses IPv6; reads "1.2.3.4" as an IP) |
| Approval policy: the right web searches wait for approval | code (`sensitive_reason`) | 100% |

A metric blocks a PR when it drops below `main` by more than 2 points or one case (any drop, for the code-only suites); a case that regressed is run twice more and fails on a majority. A judged metric only gates once the judge agrees with labelled answers at least 90% of the time (`make eval-calibrate`: citations 98%, faithfulness 91%, correctness 90%). The labels and reference answers were written by Claude, not a person. With the Langfuse keys set, each eval run is traced and gets a score per metric, so a failed case links to its run. Details in [the Phase 7 doc](docs/phase-7-eval-gate.md).

## Development

```bash
make check    # ruff lint + format check, mypy, pytest (integration tests need the cluster running)
make fmt      # auto-fix lint and formatting
make eval     # every eval suite against the real model; writes tests/evals/report.json
make eval-calibrate  # how often the judge agrees with labelled answers
make eval-check      # what CI runs: code-only suites, report.json against main's
make retention  # prune old checkpoints of idle threads
```
