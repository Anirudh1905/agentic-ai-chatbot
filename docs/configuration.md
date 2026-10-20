# Configuration

[Overview](../README.md) · [How it works](how-it-works.md) · [API](api.md) · [Configuration](configuration.md) · [Development](development.md) · [Runbook](runbook.md)

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
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | unset | Tracing turns on when both are set, with `LANGFUSE_HOST`; `.env.example` has the local Langfuse's keys |
| `LANGFUSE_HOST` | unset | Tracing needs it with both keys; `.env.example` has the local Langfuse (`http://localhost:3000`) |
| `JUDGE_MODEL` | `gpt-4.1` | Scores answers in the evals; changing it means `make eval-calibrate` |
| `LOG_LEVEL` | `INFO` | `DEBUG` adds HTTP client and access logs |
