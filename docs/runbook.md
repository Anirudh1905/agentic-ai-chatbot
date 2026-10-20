# Runbook

What to do when something goes wrong with the deployed chatbot (Phase 9). Each entry: how it shows, what to check, what to do. Commands are for the local kind cluster: `k` is `kubectl --context kind-chatbot -n chatbot`, and the gateway is `http://localhost:8088`. The alerts are in `deploy/base/monitoring.yaml`; they show on the Grafana "Chatbot" dashboard, in Prometheus (`/prometheus/alerts`) and Alertmanager (`/alertmanager`). Nothing pages.

[Overview](../README.md) · [Phase 9](phase-9-reliability-deployment.md)

| Incident | First sign |
| --- | --- |
| [The model provider is down](#the-model-provider-is-down) | `ChatbotBreakerOpen`, `ChatbotModelErrors`, `ChatbotSlowModel` |
| [Embeddings or web search are down](#embeddings-or-web-search-are-down) | `ChatbotBreakerOpen` for `embeddings` or `web_search` |
| [A run is stuck waiting for approval](#a-run-is-stuck-waiting-for-approval) | A user's chat shows an approval nobody answers, or 409s on a thread |
| [A PII leak is reported](#a-pii-leak-is-reported) | `ChatbotPIIInReplies`, or a user report |
| [Memory pushes fail](#memory-pushes-fail) | `ChatbotMemoryPushesDeadLettered`, `ChatbotMemoryQueueStuck`, `ChatbotWorkerDown` |
| [Postgres backups fail](#postgres-backups-fail) | `PostgresBackupMissing`, `PostgresWALArchiveFailing` |
| [Slow answers, or the node runs out of memory](#slow-answers-or-the-node-runs-out-of-memory) | `ChatbotSlowFirstToken`, pods `OOMKilled`, probes timing out |
| [Nothing works after Docker restarts](#nothing-works-after-docker-restarts) | Pods crash-looping, `connection refused` to `10.96.0.1:443` |

## The model provider is down

**How it shows:** `ChatbotBreakerOpen` with `dependency="gpt-5.4"` (after a minute open), often after `ChatbotModelErrors` or `ChatbotSlowModel`. Users mostly don't notice: answers come from the fallback model.

**What the app does on its own** ([Model calls](phase-9-reliability-deployment.md#model-calls)): each failed call is retried once, then goes to `gpt-4.1`. After 5 outages in 60 s the breaker opens and calls go straight to `gpt-4.1` for 30 s; then one call probes, and its success closes the breaker. A reply that failed partway is redrawn (`reset`). `make outage-test` rehearses all of it.

**Check:**

- **Which models:** the dashboard's "Circuit breakers open" and "Model call errors / min" panels. Only the primary means the fallback is carrying the load; `gpt-4.1` too means the gateway itself is down, and chats fail with an error after the retries.
- **What kind of failure:** `k logs deploy/api | grep -i -E "breaker|retry|fallback"`: timeouts (the gateway stalls), 429s (its rate limit) or 5xx.
- **The gateway itself,** from the host: `curl -s "$OPENAI_BASE_URL/models" -H "api-key: $OPENAI_API_KEY"`.

**Do:**

- **Only the primary is down:** nothing; watch it recover (the breaker closes by itself). Tell users answers may differ in style for a while.
- **It's down for long:** switch the primary for a while, so calls stop waiting on its retries: add `MODEL=gpt-4.1` to the `app-config` literals in `deploy/local/kustomization.yaml` (Tilt applies it and rolls the pods). Remove it after.
- **Everything is down** (the gateway, or both models): nothing in the app can fix it. Chats fail with an error; users can resend or resume later, since checkpoints keep each thread. Users with their own key (`credentials` at login) aren't affected: their calls never go through ours.
- **429s:** the gateway's quota. Lower the load: `DAILY_TOKEN_BUDGET`, `RATE_LIMIT_PER_USER`.

## Embeddings or web search are down

**How it shows:** `ChatbotBreakerOpen` for `embeddings` or `web_search`. Chats keep working.

**What the app does:** without embeddings, knowledge base search runs on full text only and the answer says so; memory recall goes on with what the thread already has. Without web search, the tool tells the model it's unavailable, and the model answers from the knowledge base or says it can't check.

**Do:** nothing to restore by hand; the breaker probes every 30 s. For web search, check whether DuckDuckGo is rate-limiting this IP (`No results found.` in the API logs): the web search cache (`WEB_SEARCH_CACHE_SECONDS`) takes repeats off it. Don't run `make ingest` while the embeddings are down; it fails at once.

## A run is stuck waiting for approval

**How it shows:** a user's chat shows an approval request (a sensitive web search) that nobody answers, or every message on a thread gets 409 "still answering".

**Check:**

- **A 409 that doesn't clear:** a run's lease lasts 60 s after its last renewal, so a crashed run frees its thread within a minute. A 409 for longer means a run is genuinely still going: look for it in `k logs deploy/api`.
- **A run paused for approval** holds no lease, so it isn't what's blocking; it waits for the user's decision forever. Threads whose latest checkpoint is waiting:

  ```bash
  k exec postgres-1 -c postgres -- psql -U postgres -d chatbot -c "
    SELECT t.id, t.user_id, t.updated_at FROM threads t
    JOIN (SELECT thread_id, max(checkpoint_id) AS latest FROM checkpoints
          WHERE checkpoint_ns = '' GROUP BY thread_id) c ON c.thread_id = t.id::text
    WHERE EXISTS (SELECT 1 FROM checkpoint_writes w
                  WHERE w.thread_id = c.thread_id AND w.checkpoint_id = c.latest
                    AND w.channel = '__interrupt__')
    ORDER BY t.updated_at"
  ```

- **A run that stopped partway** (a pod died mid-tool call) isn't paused: the UI shows the reply cut off, and the next message works, with the dangling tool call closed.

**Do:**

- **The user's own decision is best:** they approve, edit or reject in the UI, or `POST /threads/{id}/resume` with `decisions`. A paused thread doesn't block new chats.
- **On the user's behalf** (they asked, or the query must not run): reject it. `POST /threads/{id}/resume` with `{"decisions": [{"type": "reject", "message": "Cancelled by support"}]}` and a token for that user, minted in an API pod:

  ```bash
  k exec deploy/api -- python -c "
  from chatbot.config import get_settings; from chatbot.api.security import create_access_token
  print(create_access_token(get_settings(), '<user_id>')[0])"
  ```

  Or delete the thread (`DELETE /threads/{id}` with the same token).
- **A run cut off mid-stream:** `POST /threads/{id}/resume` with no body finishes it from its checkpoint.

## A PII leak is reported

**How it shows:** `ChatbotPIIInReplies` (the model wrote an email, card number, phone or IP in a reply; it was redacted before it reached the user), or a user saying the bot showed or kept personal data.

**Contain, first:**

1. **Find the run.** In Langfuse, traces carry the thread as their session and a hashed user id (`hashed_user` in `observability/tracing.py`, an HMAC with `JWT_SECRET`). From the alert's time and type, look for traces with a redaction placeholder (`[REDACTED_EMAIL]` and the like) in the reply.
2. **Remove what was stored.** The thread: `DELETE /threads/{id}` (with the user's token, minted as above); its memories: `DELETE /me/memory` or `DELETE /me/memory/{id}`; everything: `DELETE /me`. These delete the checkpoints too.
3. **Remove it from the traces:** delete the trace in Langfuse (its UI, or the public API). Metrics and logs hold no message text.
4. **If the data reached a third party** (it was in a web search query, sent before approval): note the time and query; the sensitive-terms approval (`HITL_SENSITIVE_TERMS`) is what should have stopped it.

**Then, why:**

- **A redaction gap:** add the case to `tests/evals/pii_cases.jsonl`, fix `guardrails/pii.py`, and run `make eval`: the PII suites must not drop.
- **A model reply with PII from a tool result:** check that the tool's output is redacted (the PII middleware runs on tool results too).
- **Record it:** what leaked, to whom, when, and what was deleted.

## Memory pushes fail

**How it shows:** `ChatbotMemoryPushesDeadLettered` (a push failed 5 times), `ChatbotMemoryQueueStuck` (pushes waiting 15 minutes with none run), or `ChatbotWorkerDown`. Chats work; new memories wait.

**Check:** `k logs deploy/worker`, and the queue's depth by state in Prometheus: `chatbot_queue_messages` (visible, in flight, dead).

**Do:**

- **The worker is down:** `k rollout restart deploy/worker`. Pushes wait in the queue meanwhile and nothing is lost.
- **Dead-lettered pushes:** read why in the worker's logs (usually the memory model failing). Once fixed, move them back: each message holds the whole push, so sending its body back to `memory-pushes.fifo` replays it (ElasticMQ on `localhost:9324`, `aws sqs receive-message` / `send-message` with `--endpoint-url`).

## Postgres backups fail

**How it shows:** `PostgresBackupMissing` (no base backup in 26 h) or `PostgresWALArchiveFailing` (critical: a restore would lose everything since the last archived WAL, and `pg_wal` grows).

**Check:** MinIO is up (`k get pods -l app=minio`); the archiver's errors (`k logs postgres-1 -c plugin-barman-cloud`); the backups (`k get backups.postgresql.cnpg.io`).

**Do:** fix MinIO or its credentials (`backup-store` Secret), then take a base backup at once: `k apply` a `Backup` for cluster `postgres` with `method: plugin` (as in `deploy/base/backup.yaml`). To restore: `make restore TARGET="<time>"` into a second cluster, check its counts, and switch over by hand ([Backups](phase-9-reliability-deployment.md#backups)). `make restore-test` proves restores work.

## Slow answers, or the node runs out of memory

**How it shows:** `ChatbotSlowFirstToken`; on the kind node, probes timing out everywhere, `OOMKilled` pods, Grafana answering "upstream request timeout".

**Check:** the dashboard's model call latency against time to first token. If model calls are fast and first tokens slow, it's the knowledge base search: the reranker runs on the API pods' CPU, one rerank at a time per pod, and was the limit in the [load test](phase-9-reliability-deployment.md#load-test). Memory: `docker stats`, and `k describe pod` for `OOMKilled`.

**Do:**

- **Too much load for the node:** fewer users, or a bigger Docker VM. On this laptop the measured capacity is 10 concurrent users on 8 CPUs and 24 GB, and 5 on 6 CPUs and 16 GB.
- **Reranking is the bottleneck:** turn it off (add `RERANKER_MODEL=` to the `app-config` literals): searches use the fused order, slightly worse at top-1 (96% against 100%), much faster under load.
- **Out of memory:** stop other containers sharing the Docker VM, or give the VM more memory. The API pods' 3 Gi limit makes one of them, not Postgres, the pod that's killed.

## Nothing works after Docker restarts

**How it shows:** after Rancher Desktop or Docker restarts, kube-proxy fails with "too many open files", pods can't reach the API server (`dial tcp 10.96.0.1:443: connect: connection refused`), and Postgres, the operators and the gateway crash-loop. Tilt shows resources in error from when they failed.

**Do:**

1. Check `docker info` answers. After a Rancher Desktop restart, `/var/run/docker.sock` can point at Docker Desktop's socket instead of Rancher's: `export DOCKER_HOST=unix://$HOME/.rd/docker.sock`, or fix the socket in Rancher Desktop's settings.
2. `make cluster`: it raises the VM kernel's inotify limits, which a restart resets.
3. Delete the pods in `CrashLoopBackOff` (`kubectl --context kind-chatbot get pods -A`), so they start again now rather than after their back-off.
4. In Tilt, re-run any resource left in error (`tilt trigger postgres`), so the ones waiting on it roll out.
5. `deploy/smoke.sh` to check.
