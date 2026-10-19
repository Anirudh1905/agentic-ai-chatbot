# Phase 9: Reliability, Cost and Deployment

**Goal:** Keep answering when a provider fails, keep cost and latency in check, and deploy the whole stack repeatably.

[← Phase 8](phase-8-security-observability.md) · [Overview](../README.md)

**Status: in progress.** Deployment is designed (below) and built first; reliability, cost and latency, and proof follow, in that order, each designed when it starts. Like Phases 7 and 8, this is for a production-ready demo: a local Kubernetes cluster, no real users or on-call. Items already done in earlier phases are ticked.

| Area | Planned | Section |
| --- | --- | --- |
| Reliability | Retries and circuit breakers on every external call, a fallback model, a per-thread lock, Postgres backups | [Reliability](#reliability) |
| Cost and latency | Prompt caching, a cap on retrieved context, a web search cache | [Cost and latency](#cost-and-latency) |
| Deployment | The app on Kubernetes (kind): an image, 2 API replicas behind an Ingress, a worker, a migrations Job, CloudNativePG, kube-prometheus-stack | [Deployment (design)](#deployment-design) |
| Proof | A load test, a provider-outage test, a runbook | [Exit criteria](#exit-criteria) |

The target shape is in [What runs where](#what-runs-where).

API pods are stateless. All conversation state lives in Postgres through the checkpointer, so any pod can serve any thread, and a pending approval can be resumed on a different pod from the one that created it.

Most items below have LangChain middleware equivalents (`ModelRetryMiddleware`, `ModelFallbackMiddleware`, `ToolCallLimitMiddleware`). Check `langchain.agents.middleware` before building one.

## Deployment (design)

**Status: designed, not built.** Agreed on 2026-10-19. The first piece of Phase 9 to build, because the other three are measured on it: the load test and the outage test need the deployed stack, and the per-thread lock and the background loops depend on where processes run.

### Decisions

| Decision | Chosen | Why, and what it rules out |
| --- | --- | --- |
| Target | Kubernetes in Phase 9, on a local **kind** cluster | Replicas, a migration step that gates the rollout and scheduled jobs are what Kubernetes is for; Compose would only approximate them. There is no separate Phase 10 |
| Our manifests | **Kustomize**: `deploy/base` and a `deploy/local` overlay | Plain YAML, applies with or without Tilt (`kubectl apply -k deploy/local`) |
| Third-party services | **Helm** charts, installed once by Tilt | Only where hand-written manifests would be painful: ingress-nginx, CloudNativePG, kube-prometheus-stack |
| In the cluster | The app, Postgres, Redis, Prometheus and Grafana | **Langfuse stays in Compose**: it's heavy (about five services) and a real deployment often rents it as Langfuse Cloud |
| Postgres | **CloudNativePG**, one instance with pgvector | The production-grade choice (failover, backups to object storage); the later backup work becomes configuration |
| Redis | A plain Deployment, no volume | It holds only rate-limit windows and token counts, which may be lost on restart (Phase 8) |
| Monitoring | **kube-prometheus-stack** | A `ServiceMonitor` scrapes every API pod whatever the replica count; the 7 alerts become a `PrometheusRule`; Alertmanager is ready to route (Phase 8 had nothing to page with) |
| Background work | Every memory push goes through a **queue**; a **worker** Deployment (1 replica) runs them, the idle-thread scan and the store's TTL sweeper | Today a push runs as a task in the API pod that triggered it. If that pod dies mid-push, a summary push is lost for good: the summarized turns have already left the thread, and the memory writer skips the summary text (`transcript` in `memory/long_term/writer.py`). "Remember this" answers 202 and can lose its push the same way. With 2 replicas, rolling deploys and a 30 s drain, that goes from rare to routine. A queue also takes the memory model's calls, and the pooled connection each push holds, out of the API pods |
| Queue | **Amazon SQS**, with **ElasticMQ** standing in for it in the cluster, CI and dev | The same API locally and on AWS. ElasticMQ is one small container with FIFO queues, visibility timeouts, dead-letter queues and on-disk persistence. LocalStack emulates far more than we need. A Redis Streams queue would have made Redis durable; it stays the disposable limits store |
| Schema | A **migrations Job** runs Alembic, `checkpointer.setup()` and `store.setup()` | Once, before the API rolls out; a failed migration stops the rollout instead of breaking pods. API and worker no longer create tables at start |
| Reranker model (about 570 MB) | A **shared volume**, filled once by a Job, mounted read-only | The image stays small and the model is downloaded once, not per pod |
| Traffic | **ingress-nginx**, kind mapping host port 80 | `http://localhost/` is the UI, `/api` the API across both replicas; the load test goes through the same front door as users |
| Settings and secrets | Kustomize **configMapGenerator** and **secretGenerator** | The Secret is built from a gitignored `deploy/local/secrets.env` and mounted at `/run/secrets` (read since Phase 8); a changed value rolls the pods, through the generated name's hash |
| API replicas | **2, fixed**, with a PodDisruptionBudget (`minAvailable: 1`) and rolling updates at `maxUnavailable: 0` | Autoscaling waits for the load test: the API mostly waits on the model gateway, so CPU is the wrong signal; scaling on in-flight requests needs the Prometheus adapter |
| Shutdown | **Graceful drain** | Readiness fails, `preStop` waits a few seconds, uvicorn finishes open streams (`--timeout-graceful-shutdown` 45 s in a 60 s grace period), background pushes get up to 30 s. A stream cut off anyway resumes from its checkpoint (`POST /threads/{id}/resume`) |
| Dev workflow | **`tilt up` deploys to kind** | One environment everywhere; Tilt's live update syncs `src/` into the pods instead of rebuilding the image |
| CI | **Build and validate, plus a kind smoke test** | Most Kubernetes mistakes (probes, Secrets, Service names, migration order) only show when the pods start |

### What runs where

```mermaid
flowchart TB
    Browser([Browser]) -->|"http://localhost"| Ingress[ingress-nginx]
    subgraph Cluster["kind cluster"]
        Ingress -->|"/"| UI["ui · Deployment ×1<br/>Streamlit"]
        Ingress -->|"/api (not /api/metrics)"| API["api · Deployment ×2<br/>PDB · rolling · graceful drain"]
        UI -->|"http://api:8000"| API
        Prom["kube-prometheus-stack<br/>Prometheus · Grafana · Alertmanager"] -->|"ServiceMonitor: every pod"| API
        API --> Redis[("Redis<br/>no volume")]
        API --> PG[("Postgres<br/>CloudNativePG + pgvector")]
        API -.-|"read-only"| ModelPVC[("reranker model volume")]
        Model["reranker-model Job"] --> ModelPVC
        API -->|"enqueue push"| SQS[("ElasticMQ · SQS API<br/>memory-pushes.fifo + DLQ")]
        SQS -->|"long poll"| Worker["worker · Deployment ×1<br/>memory pushes · idle scan · TTL sweeper"]
        Worker --> PG
        Jobs["migrate Job · retention CronJob · ingest Job"] --> PG
    end
    API -->|"chat · embeddings"| Gateway[Model gateway]
    Worker -->|"memory model"| Gateway
    API -->|web_search| DDG[DuckDuckGo]
    API -.->|run traces| LF["Langfuse<br/>Docker Compose"]
```

- **One app image** (`Dockerfile`, a multi-stage uv build, `.env` excluded by `.dockerignore`) runs as the API, the worker, the UI and every Job, each with its own command.
- **API:** a Deployment of 2, a Service, and the Ingress at `/api`. The Ingress strips the prefix (the routes stay at `/chat`, `/threads`, …) and uvicorn runs with `--root-path /api` so the OpenAPI docs work behind it. **`/api/metrics` is refused at the Ingress**: Prometheus scrapes the pods directly, and the endpoint isn't authenticated or limited (Phase 8). `/healthz` stays the liveness probe; a new **`/readyz`** (the database pool answers) is the readiness probe, so a pod that loses Postgres leaves the Service instead of being restarted.
- **The queue:** one FIFO queue, `memory-pushes.fifo`, and its dead-letter queue, `memory-pushes-dlq.fifo`, after 5 failed receives. ElasticMQ declares both in its config at start and runs as a StatefulSet with message persistence on a volume, so queued pushes survive a restart, as on SQS.
  - **One job per push**, as a small JSON message:
    - a *summary* push carries the transcript itself (already PII-redacted), because the turns have left the thread; a few thousand tokens, far under SQS's 256 KB limit
    - an *idle* push carries the thread id; the worker reads the thread and pushes what's after its cursor
    - a *remember* push carries the question and the reply
  - **`MessageGroupId` is the user**, so one user's pushes run in order, one at a time. That is the guarantee the per-user advisory lock gives today, and the lock stays as a second guard.
  - **`MessageDeduplicationId`** is the thread and its last message id (or the remembered message's id), so the same push enqueued twice within SQS's 5-minute window runs once. Idle pushes are idempotent anyway, through the cursor.
  - **Code:** a `JobQueue` protocol (`enqueue`, `receive`, `ack`) in a new `chatbot/jobs/` package, implemented on SQS with `aioboto3`, behind `SQS_ENDPOINT_URL` (ElasticMQ; unset on AWS) and `SQS_QUEUE_URL`. `MemoryWriter.schedule` and `remember` enqueue instead of starting a task; the writing itself (`push`, `_locked_write`) moves unchanged into the worker. Unit tests use an in-memory `JobQueue`.
- **Worker:** a new `python -m chatbot.worker` long-polls the queue and runs each push. It deletes a message only after the push succeeds; a failure leaves the message to come back after its visibility timeout (5 minutes, longer than a push's worst case of retried model calls). The 6th failure lands in the dead-letter queue. The worker also runs the idle-thread scan, which now only enqueues idle pushes, and the store's TTL sweeper. A new setting, **`BACKGROUND_JOBS`** (`true` by default so `make dev` keeps working alone; `false` in the API pods), decides whether a process runs the scan and the sweeper.
- **Queue metrics:** the worker exports the queue's depth, the oldest message's age and the dead-letter count (`GetQueueAttributes`), and counts pushes by outcome. New alerts: a message in the dead-letter queue, and the oldest message older than 15 minutes (the worker is down or stuck).
- **UI:** one Streamlit pod at the Ingress root, calling the API in-cluster.
- **Jobs:**
  - `migrate`: a new `python -m chatbot.migrate` that runs `alembic upgrade head`, then the checkpointer's and the store's setup. Tilt runs it before the API and worker roll out; `make migrate` calls the same module.
  - `reranker-model`: downloads the model into the shared volume once; the API mounts it read-only (`RERANKER_CACHE_DIR`).
  - `retention`: a CronJob, nightly, running today's `python -m chatbot.memory.retention`.
  - `ingest`: a Job you trigger from Tilt, for `data/sample` or your own documents.
- **Langfuse** stays in `ops/langfuse/docker-compose.yml` (`make langfuse`). Its containers are attached to kind's Docker network, so pods reach `http://langfuse-web:3000`. To verify first when building: if pods can't resolve it, a Service with manual Endpoints pointing at the container's address does the same.
- **Layout:** `deploy/base/` (our manifests), `deploy/local/` (the kind overlay: generators, `secrets.env`, image tag), `deploy/kind.yaml` (the cluster, with port 80 mapped), `deploy/helm/` (values for the three charts).

### Dev workflow and settings

- `tilt up` creates the kind cluster if it's missing (Tilt's `ctlptl` integration), installs the three Helm charts once, builds the image and applies `deploy/local`. Live update syncs `src/` and restarts the process in the api, worker and ui pods; a change to `pyproject.toml` or `uv.lock` rebuilds the image. The migrate, ingest, retention, checks and evals resources stay in the Tilt UI.
- Tilt port-forwards Postgres to `localhost:5433`, Redis to `6380` and ElasticMQ to `9324`, so `make test`, `make eval` and the integration tests keep working with today's `.env`. The UI, API, Grafana and Prometheus are reached through the Ingress on `http://localhost`.
- `docker-compose.yml` keeps only Langfuse (through its include). Postgres, Redis, pgweb, Prometheus and Grafana move into the cluster; pgweb becomes an optional Deployment.
- `make secrets` writes `deploy/local/secrets.env` from `.env` (`OPENAI_API_KEY`, `OPENAI_BASE_URL`, `JWT_SECRET`, the Langfuse keys). The overlay's ConfigMap holds the rest, including the in-cluster `DATABASE_URL`, `REDIS_URL`, `SQS_ENDPOINT_URL` and `SQS_QUEUE_URL`. No app code changes for settings: Phase 8 already reads `/run/secrets` and requires the URLs.
- Running without Kubernetes stays possible with `make dev`, with Postgres and Redis started by hand. With `SQS_QUEUE_URL` unset there is no queue: pushes run in-process, as before this phase.

### CI

A second job beside today's `check`:

1. Build the image.
2. `kustomize build deploy/local` and validate it with kubeconform.
3. Start kind, install the three charts, deploy with a placeholder model key, and wait for the migrations Job.
4. Through the Ingress: `/readyz` answers, register and log in work, Prometheus has both API pods and the worker as targets, and both queues exist in ElasticMQ.

There is no chat call: CI has no model access. About 6 to 8 minutes.

### Failures

| What fails | What happens |
| --- | --- |
| The migrations Job | It shows as failed in Tilt and the API doesn't roll out, so nothing serves against the wrong schema |
| Postgres | `/readyz` fails and the pod leaves the Service; `/healthz` keeps answering, so it isn't restarted in a loop |
| Redis | Limits let requests through, as in Phase 8 |
| The queue (ElasticMQ, or SQS) | Enqueueing fails: the API retries twice, then runs that push in-process as today, logs it and counts it (`chatbot_queue_fallbacks_total`), so a push is never dropped. The worker waits and reconnects |
| The worker | Pushes wait in the queue until it's back; nothing is lost. The oldest-message alert fires after 15 minutes |
| A push keeps failing | It's retried after each visibility timeout; after 5 receives it moves to the dead-letter queue, which alerts. The message holds everything needed to replay it |
| The model volume is empty | Search uses the fused results without reranking, as the code already does when the model can't load, and logs it |
| A pod replaced mid-stream | The drain lets the stream finish; if it's cut anyway, resume finishes the run from its checkpoint |

### Tests and checks

- Unit tests:
  - `/readyz` ready and not ready
  - `BACKGROUND_JOBS=false` starts neither the idle scan nor the sweeper, and the worker starts both
  - a summary, a Remember this and the `remember` tool each enqueue one job with the right group and dedupe ids; the worker runs it and acks it; a failing push isn't acked; an enqueue failure falls back to an in-process push
  - the SQS backend against ElasticMQ, in the integration tests (CI runs ElasticMQ as a service)
  - the worker and migrate entry points, on today's in-memory fakes and against Postgres in the integration tests
- The CI smoke test above.
- Checked live before it's done:
  - chats through the Ingress spread over both API pods (the per-pod request metrics)
  - a rolling restart during a streaming reply breaks no stream
  - deleting the worker pod mid-push: the message comes back and the memory is written once
  - a push that fails 5 times reaches the dead-letter queue and fires its alert

### Implementation plan

Each step is tested and committed on its own, in order. Steps 1 to 4 are app code that also works outside Kubernetes. The cluster comes in at step 5. Each step lists what proves it done.

1. **Readiness and background switch.**
   - Add `GET /readyz`, which checks that the pool answers `SELECT 1` and returns 503 otherwise.
   - Add the `BACKGROUND_JOBS` setting, so the API pods neither start the idle-thread loop nor the store's TTL sweeper.
   - Done when: unit tests cover both `/readyz` states, and an app with `BACKGROUND_JOBS=false` starts neither. `make check` passes.
2. **Migrations as one command.**
   - `python -m chatbot.migrate` runs Alembic, `checkpointer.setup()` and `store.setup()`.
   - `make migrate` and Tilt call it.
   - `Resources.ensure` stops running `setup()`.
   - Done when: the integration tests (which migrate a throwaway database) pass with it, and the API starts against a migrated database without creating tables.
3. **The job queue.**
   - Add a `chatbot/jobs/` package:
     - the `JobQueue` protocol
     - the three job types (summary, idle, remember) as JSON
     - `SqsJobQueue` on `aioboto3`
     - `InMemoryJobQueue` for tests
     - settings `SQS_ENDPOINT_URL` and `SQS_QUEUE_URL`
   - FIFO group id is the user, plus a deduplication id per push.
   - Done when: unit tests cover encode, decode, group and dedupe ids, and integration tests run against ElasticMQ, which CI gets as a service container.
4. **Pushes through the queue, and the worker.**
   - Change `MemoryWriter.schedule`, `remember` and the idle scan to enqueue when a queue is configured. Without one, they keep running in-process as today.
   - Enqueueing retries twice, then falls back to an in-process push and counts it.
   - `python -m chatbot.worker` long-polls, runs each push with the existing writing code, acks on success, and leaves the message on failure. It also runs the idle scan and the sweeper.
   - Queue metrics: depth, the oldest message's age, the dead-letter count, pushes by outcome, fallbacks.
   - Done when: unit tests show each trigger enqueues one job with the right ids, the worker acks success and not failure, and the fallback writes the memory. The live check: a memory written through ElasticMQ on the host.
5. **The image.**
   - A multi-stage `Dockerfile` (uv, Python 3.12, non-root) with the app and the `ui` group, and a `.dockerignore` that excludes `.env`, `data/` and caches.
   - Commands for api (uvicorn with `--root-path /api` and graceful shutdown), worker, ui, migrate, ingest, retention and reranker-model.
   - Done when: `docker build` works and `docker run … chatbot.migrate --help` runs.
6. **Cluster and third-party services.**
   - `deploy/kind.yaml` maps port 80 and sets up port-forwards.
   - `deploy/helm/` holds values for ingress-nginx, CloudNativePG (operator plus a one-instance pgvector `Cluster`) and kube-prometheus-stack (lean: no node-exporter or kube dashboards).
   - Done when: the cluster comes up with the three charts healthy.
7. **Our manifests.** `deploy/base/`:
   - **api:** Deployment ×2, Service, PodDisruptionBudget, probes, `preStop`, a 60 s grace period, the model volume mounted read-only.
   - **worker:** Deployment.
   - **ui:** Deployment and Service.
   - **Ingress:** `/` → ui, `/api` → api with the prefix stripped, `/api/metrics` refused.
   - **Jobs:** migrate, reranker-model, ingest; the retention CronJob.
   - **Data:** Redis Deployment and Service; ElasticMQ StatefulSet with a volume and its queue config (both queues, the dead-letter policy).
   - **Monitoring:** the `ServiceMonitor`; a `PrometheusRule` with today's 7 alerts plus the 2 queue alerts; the dashboard ConfigMap.

   `deploy/local/`:
   - ConfigMap and Secret generators
   - the image tag
   - a `make secrets` target that writes `secrets.env` from `.env`

   Done when: `kustomize build deploy/local` passes kubeconform.
8. **Tilt into kind.**
   - Rewrite the Tiltfile:
     - create the cluster if needed
     - install the charts once
     - `docker_build` with live update for `src/`
     - `k8s_yaml(kustomize('deploy/local'))`
     - the migrate Job before the api and worker, then the reranker and ingest Jobs
     - port-forwards for Postgres, Redis and ElasticMQ
     - manual resources for checks, evals, retention and ingest
   - `docker-compose.yml` keeps only Langfuse, attached to kind's network; verify pods reach `langfuse-web:3000`, else add a Service with manual Endpoints.
   - Done when: `tilt up` from nothing serves a chat through `http://localhost`, the trace shows up in Langfuse, and Grafana shows both api pods.
9. **CI.**
   - A `deploy` job:
     - build the image
     - kustomize plus kubeconform
     - kind with the charts
     - deploy with a placeholder model key
     - wait for migrate
     - check `/readyz`, register and login, scrape targets and queues
   - Done when: it passes on the PR.
10. **Live checks and docs.**
    - Live checks, each with its result written in this doc:
      - chats spread over both api pods
      - a rolling restart during a streaming reply doesn't break it
      - deleting the worker mid-push writes the memory once
      - a push failing 5 times reaches the dead-letter queue and alerts
    - Update:
      - the README quickstart (`tilt up` into kind, the Ingress URLs)
      - the architecture diagram, with a cluster view
      - this section: designed becomes built
    - Done when: the exit criteria above are ticked.

**Execution:** I build the steps in order, with tests first where there's logic (steps 1 to 4). I commit each step, run `make check` before each commit, and stop to report after steps 4, 8 and 10, or earlier if a step turns out different from the design.

### Not in this piece

- **Reliability, then cost and latency, then proof**, in that order, each with its own design ([below](#reliability)).
- **Production gaps, to document, not build:** the real SQS queues and their IAM permissions (Terraform), in place of ElasticMQ; autoscaling on in-flight requests; Sealed Secrets or External Secrets instead of a generated Secret; storage several nodes can read, for the model volume on a multi-node cluster; Alertmanager routing; TLS on the Ingress.

## Reliability

- [ ] Retries with backoff and circuit breakers on the model, web search and embedding calls. Searches already retry (`ToolRetryMiddleware`, `tools/registry.py`); the model needs `ModelRetryMiddleware` or the client's `max_retries`; circuit breakers are still to add
- [ ] Fallback model on provider outage (`ModelFallbackMiddleware`). The gateway stalls gpt-5 calls under load (Phase 6 and 7 measurements), so this matters here
- [x] Graceful degradation: web search down → answer from the knowledge base and say so (`ToolRetryMiddleware` returns an error result the model relays)
- [ ] A per-thread lock: two requests on one thread both read the same checkpoint and the later write wins (Phase 3). The UI sends one message at a time
- [ ] Backups and a tested restore for Postgres

## Cost and latency

- [ ] Prompt caching on the system prompt and tool definitions (Phase 6 keeps the recalled memories stable between turns for this)
- [ ] Cap on retrieved context size
- [ ] Cache frequent web search queries (short TTL)
- [x] Small model for summarization (`gpt-4.1-mini`, Phase 3); query rewriting could use it too. Memory extraction stays on `gpt-5-mini` (Phase 6: nano kept outdated and forbidden facts)
- [x] Cap on model and tool round trips (`ModelCallLimitMiddleware`, `MAX_MODEL_CALLS`; `ToolCallLimitMiddleware` if one tool needs its own cap)
- [x] Summaries made after the reply, not before it (Phase 3)
- [x] Per-user rate limits and a daily token budget (Phase 8)

## Exit criteria

- [ ] `tilt up` brings up the whole stack on kind; a chat works through the Ingress, spread over 2 API pods
- [ ] A rolling restart of the API breaks no streaming reply
- [ ] The worker alone runs the background loops; Prometheus scrapes every API pod
- [ ] CI builds the image, validates the manifests and passes the kind smoke test
- [ ] Load test: target concurrent users at the p95 latency target with no errors
- [ ] A provider outage degrades to the fallback model, not errors
- [ ] Runbook for common incidents: provider outage, stuck interrupts, a PII leak report
