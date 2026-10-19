# Phase 9: Reliability, Cost and Deployment

**Goal:** Keep answering when a provider fails, keep cost and latency in check, and deploy the whole stack repeatably.

[← Phase 8](phase-8-security-observability.md) · [Overview](../README.md)

**Status: planned.** Like Phases 7 and 8, this is for a production-ready demo: one host or a small cluster, no real users or on-call. Items already done in earlier phases are ticked.

| Area | Planned | Section |
| --- | --- | --- |
| Reliability | Retries and circuit breakers on every external call, a fallback model, a per-thread lock, Postgres backups | [Reliability](#reliability) |
| Cost and latency | Prompt caching, a cap on retrieved context, a web search cache | [Cost and latency](#cost-and-latency) |
| Deployment | An app image, Compose for the whole stack, migrations as a deploy step, one background worker, several API processes scraped by Prometheus | [Deployment](#deployment) |
| Proof | A load test, a provider-outage test, a runbook | [Exit criteria](#exit-criteria) |

```mermaid
flowchart TB
    Users([Users]) --> LB[Load balancer / TLS]
    LB --> API1[API pod]
    LB --> API2[API pod]
    LB --> APIn[API pod ...]

    API1 & API2 & APIn --> PG[(Postgres + pgvector<br/>checkpoints, store,<br/>threads, chunks)]
    Worker[Background worker<br/>idle-thread memory pushes,<br/>retention, ingestion] --> PG

    API1 & API2 & APIn --> LLMAPI[Model gateway]
    API1 & API2 & APIn --> Search[DuckDuckGo]

    API1 & API2 & APIn -.-> Obs[Langfuse<br/>+ Prometheus + Grafana]
    Worker -.-> Obs
```

API pods are stateless. All conversation state lives in Postgres through the checkpointer, so any pod can serve any thread, and a pending approval can be resumed on a different pod from the one that created it.

Most items below have LangChain middleware equivalents (`ModelRetryMiddleware`, `ModelFallbackMiddleware`, `ToolCallLimitMiddleware`). Check `langchain.agents.middleware` before building one.

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

## Deployment

- [ ] An app image, built with `.env` excluded (secrets come from the environment or `/run/secrets`, Phase 8), and Compose for the whole stack: API, UI, Postgres, Langfuse
- [ ] DB migrations (Alembic, today `make migrate`) as their own step before the new version starts
- [ ] One background worker for the idle-thread memory loop, retention and ingestion. Today every API process runs the memory loop (Phase 6), which the Postgres advisory lock keeps safe across replicas, and `make retention` is run by hand. A job queue (Redis) is only needed if pushes outgrow one worker
- [ ] Prometheus scrapes every API process and the dashboard sums them. Phase 8 built the metrics, dashboard and alerts for a single process, and its counts are per process
- [ ] `/metrics` reachable only from the cluster network (it isn't authenticated or limited)

## Exit criteria

- [ ] Load test: target concurrent users at the p95 latency target with no errors
- [ ] A provider outage degrades to the fallback model, not errors
- [ ] Runbook for common incidents: provider outage, stuck interrupts, a PII leak report
