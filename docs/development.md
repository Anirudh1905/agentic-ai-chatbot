# Development

[Overview](../README.md) · [How it works](how-it-works.md) · [API](api.md) · [Configuration](configuration.md) · [Development](development.md) · [Runbook](runbook.md)

## Repository layout

```text
src/chatbot/
├── config.py       # every setting (env, /run/secrets, .env), and RunContext (the run's user)
├── agent/          # builder.py (create_agent + middleware), middleware.py (prompt, call limit, the model's circuit breaker, the user's own model, metrics), approvals.py (HITL rules)
├── api/            # main.py, routes/ (auth, chat, threads, me, health), security.py (the JWT and the signed-in user), middleware.py (request log and metrics), runs.py, streaming.py (SSE), leases.py (one run per thread), drain.py (the pod's preStop hook), schemas.py, dependencies.py
├── accounts/       # users.py (email and argon2id hash), limits.py (rate limits and the daily token budget, Redis)
├── guardrails/     # pii.py: redaction rules, PIIMiddleware for outputs, the SSE stream redactor
├── memory/         # short_term.py (summarization), long_term/ (user memories: facts, store, writer, middleware), threads.py, retention.py
├── tools/          # rag.py (knowledge base), web_search.py, rerank.py, memory.py (remember), registry.py
├── ingestion/      # loaders, chunking, indexing CLI
├── prompts/        # system.md, summarize.md, memory_extract.md, memory_decide.md; as_data (defuses untrusted text)
├── jobs/           # push.py (a memory push as a queue message), queue.py (JobQueue: SQS, in memory)
├── reliability/    # breaker.py (circuit breakers on the models, the embeddings and web search, in Redis)
├── observability/  # logs.py (request and thread on every line), tracing.py (Langfuse), metrics.py (Prometheus)
├── storage/        # db.py (Postgres pool), embeddings.py, langgraph.py (the checkpointer and the store)
├── migrate.py      # Alembic, then the checkpointer's and the store's tables
└── worker.py       # runs the memory pushes from the queue, the idle-thread scan and the TTL sweeper
ui/app.py           # Streamlit chat UI: login, threads, approval card, memory panel, account deletion
migrations/         # Alembic: chunks, threads, users, the thread lease (checkpoints and the memory store create their own tables)
deploy/             # kind.yaml, cluster.sh, helm/ (chart values), base/ (our manifests), local/ (the kind overlay), ci/, smoke.sh,
                    # restore.sh and restore-test.sh (backups), outage-test.sh and outage/ (the failing proxy)
ops/langfuse/       # Langfuse's compose file
Dockerfile          # the one app image: API, worker, UI and every Job
Tiltfile            # what make tilt runs, in order, and the port-forwards
Makefile            # every command below
.github/workflows/  # ci.yml: the checks, the eval gate, and the kind deploy and smoke test
pyproject.toml, uv.lock, alembic.ini
.archify/           # the diagrams' sources (candidate.json) and pages, for docs/images/{architecture,memory,knowledge-base,agent-middleware}-dark.png
tests/              # unit/, integration/ (need the cluster running), evals/ (golden sets, judge, report), load/, outage/;
                    # fakes.py (the fake models and stores) and apiclient.py (the API in-process) are shared
data/sample/        # the fictional Acme Outfitters docs
docs/               # phase plans and as-built docs, images/
```

## Services

What `make tilt` runs, and where to reach it:

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

Everything in the cluster comes in through one gateway on port 8088 (80 and 8080 are often taken). The host ports are for the tests, `make eval` and database tools, with the URLs in `.env.example`; pgweb browses the same database (chunks, threads, checkpoints, users, the memory store). A code change rebuilds the image and rolls the pods in about 20 s (2026-10-19, on the kind cluster). Details, and what each piece is for, in [the Phase 9 doc](phase-9-deployment.md).

Without Kubernetes: start Postgres and Redis yourself, then `make migrate`, `make ingest`, `make dev` and `make ui`. Without `SQS_QUEUE_URL`, memory pushes run in the API process instead of a worker.

## Evals

Every PR is checked for whether it made the bot worse. The model endpoint is an internal gateway that GitHub's runners can't reach, so the gate is split:

- **`make eval`**, on a machine that can reach the gateway, runs every suite and writes `tests/evals/report.json`, which is committed with the PR. It uses every setting at its default, whatever `.env` says.
- **CI** (`make eval-check`, no model calls) runs the code-only suites again, fails if `report.json` was made for different code, prompts, knowledge base or evals than the PR's, and compares it with the report on `main`.

| Suite | Scored by | On `main` |
| --- | --- | --- |
| Retrieval: the right section in the top 6 | code, 28 labelled questions | 100% |
| Routing: exactly the right tools | DeepEval `ToolCorrectnessMetric` (code), 44 questions | 100% |
| Faithfulness: every claim supported by that turn's search results | DeepEval `FaithfulnessMetric`, judge `gpt-4.1` | 75% (the judge is strict; see the Phase 7 doc) |
| Citations: what each citation covers is in the result it names | DeepEval `FaithfulnessMetric` per citation | 88% |
| Correctness: agrees with a reference answer | DeepEval `GEval`, 30 knowledge base questions | 100% |
| PII: labelled values redacted, look-alikes left alone | code (`redact_pii`) | 95% recall, 92% clean (misses IPv6; reads "1.2.3.4" as an IP) |
| Approval policy: the right web searches wait for approval | code (`sensitive_reason`) | 100% |

A metric blocks a PR when it drops below `main` by more than 2 points or one case (three cases for the judged metrics; any drop for the code-only suites); a case that regressed is run twice more and fails on a majority. A judged metric only gates once the judge agrees with labelled answers at least 90% of the time (`make eval-calibrate`: citations 98%, faithfulness 91%, correctness 90%). The labels and reference answers were written by Claude, not a person. With the Langfuse keys set, each eval run is traced and gets a score per metric, so a failed case links to its run. Details in [the Phase 7 doc](phase-7-eval-gate.md).

## Make targets

```bash
make check    # ruff lint + format check, mypy, pytest (integration tests need the cluster running)
make lint / make typecheck / make test / make test-unit   # the parts of make check on their own
make fmt      # auto-fix lint and formatting
make cluster / make cluster-delete  # the kind cluster and its charts (safe to re-run) / remove it
make secrets  # deploy/local/secrets.env from .env; backup-store.env (generated once) and ca-bundle.pem (this machine's CAs)
make tilt     # everything on kind
make langfuse / make down           # Langfuse in Docker Compose (make up is the same) / stop it
make dev / make ui / make worker    # without Kubernetes: the API, the UI, the worker (needs SQS_QUEUE_URL)
make migrate / make ingest          # the schema / load documents (PATH_TO_DOCS)
make eval     # every eval suite against the real model; writes tests/evals/report.json
make eval-calibrate  # how often the judge agrees with labelled answers
make eval-check      # what CI runs: code-only suites, report.json against main's
make retention  # prune old checkpoints of idle threads
make load     # load test through the gateway (USERS, DURATION)
make outage-test  # gpt-5.4 fails at a proxy; chats must finish on the fallback
make restore TARGET="2026-10-20 09:30:00+00"  # restore the backups into a second cluster
make restore-test # point-in-time restore of the Postgres backups
```
