"""Prometheus metrics, served at GET /metrics (Phase 8), or on METRICS_PORT if set (Phase 9).

Prometheus scrapes them (in the cluster, with the alert rules in deploy/base/monitoring.yaml)
and Grafana charts them (the "Chatbot" dashboard). Labels are kept to small fixed sets (route
templates, tool and model names, PII types), never users or threads: those are in the traces
(tracing.py).

The counts are per process: Prometheus scrapes every API pod and the worker, and the dashboards
sum them.
"""

from prometheus_client import Counter, Gauge, Histogram

# Seconds. Agent runs take several model calls, so the buckets go well past a web request's.
SLOW_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89)

REQUESTS = Counter(
    "chatbot_http_requests_total", "HTTP requests answered", ["method", "route", "status"]
)
REQUEST_SECONDS = Histogram(
    "chatbot_http_request_duration_seconds",
    "Time to answer a request; for a stream, until its last event",
    ["method", "route"],
    buckets=SLOW_BUCKETS,
)
FIRST_TOKEN_SECONDS = Histogram(
    "chatbot_time_to_first_token_seconds",
    "From the start of a streamed run to the first token of its reply",
    buckets=SLOW_BUCKETS,
)
ACTIVE_STREAMS = Gauge(
    "chatbot_active_streams",
    "SSE streams open right now; the API pod's preStop waits for 0 (api/drain.py)",
)
RUN_ERRORS = Counter(
    "chatbot_run_errors_total",
    "Agent runs that ended in an error (a stream's error event, or a 500)",
    ["kind"],  # "user_model": the user's own key was refused; "internal": anything else
)

MODEL_CALL_SECONDS = Histogram(
    "chatbot_model_call_duration_seconds",
    "One call to the chat model (the agent's, not summaries or memory pushes)",
    ["model"],
    buckets=SLOW_BUCKETS,
)
MODEL_CALL_ERRORS = Counter(
    "chatbot_model_call_errors_total", "Chat model calls that raised", ["model"]
)
MODEL_TOKENS = Counter(
    "chatbot_model_tokens_total", "Tokens of the agent's model calls", ["model", "kind"]
)
# Apart from MODEL_TOKENS, so its sums stay input plus output.
MODEL_CACHED_TOKENS = Counter(
    "chatbot_model_cached_tokens_total",
    "Input tokens of the agent's model calls served from the endpoint's prompt cache (Phase 9)",
    ["model"],
)

TOOL_CALLS = Counter(
    "chatbot_tool_calls_total",
    "Tool calls, after retries; outcome is ok or error",
    ["tool", "outcome"],
)
WEB_SEARCH_CACHE = Counter(
    "chatbot_web_search_cache_total",
    "Web searches looked up in the cache (Phase 9); result is hit or miss",
    ["result"],
)
APPROVAL_PAUSES = Counter(
    "chatbot_approval_pauses_total", "Runs that paused for a human to approve a tool call"
)
APPROVAL_DECISIONS = Counter(
    "chatbot_approval_decisions_total", "Answers to approval requests", ["decision"]
)
PII_REDACTIONS = Counter(
    "chatbot_pii_redactions_total",
    "PII values redacted, by type and where: the user's message, a tool result or a reply",
    ["type", "source"],
)
MEMORY_RECALLS = Counter(
    "chatbot_memory_recalls_total",
    "Turns that searched the user's long-term memories; result is hit or miss",
    ["result"],
)
LIMITED = Counter(
    "chatbot_limited_total",
    "Requests refused: 429 for a rate limit (user, ip, login) or the token budget, 409 for a "
    "thread still answering (thread_busy)",
    ["limit"],
)
LEASE_RENEWAL_FAILURES = Counter(
    "chatbot_thread_lease_renewal_failures_total",
    "Runs that couldn't renew their thread's lease (the database unreachable, or the lease "
    "lost); the run carries on",
)

# Memory pushes and their queue (Phase 9). The worker runs the pushes and exports the queue's
# depth; the API counts the pushes it couldn't enqueue and ran itself.
MEMORY_PUSHES = Counter(
    "chatbot_memory_pushes_total",
    "Long-term memory pushes run; outcome is ok, or failed (left on the queue to retry)",
    ["kind", "outcome"],  # kind: summary, idle, remember
)
QUEUE_FALLBACKS = Counter(
    "chatbot_queue_fallbacks_total",
    "Pushes the API couldn't enqueue and ran in-process instead",
)
QUEUE_DEPTH = Gauge(
    "chatbot_queue_messages",
    "Messages on the memory-push queue: visible (waiting), in_flight, dead (dead-letter queue)",
    ["state"],
)
QUEUE_JOB_AGE = Histogram(
    "chatbot_queue_job_age_seconds",
    "How long a push waited on the queue before the worker received it",
    buckets=(0.5, 1, 5, 15, 60, 300, 900, 3600),
)

# Circuit breakers (Phase 9, reliability/breaker.py). The state is shared in Redis; each
# process reports what it last saw, so the dashboard takes the max. (Calls to the fallback
# model show in the model call metrics, under its name.)
BREAKER_OPEN = Gauge(
    "chatbot_breaker_open",
    "1 while a dependency's circuit breaker is open (a model, embeddings, web_search)",
    ["dependency"],
)

# Every fixed label set starts at 0. A series that first appears already at 1 has no earlier
# sample, so rate() and increase() miss that first count, and a ratio with no matching series
# is blank instead of 0. (PII types and tool names start at 0 where they're defined.)
for _kind in ("user_model", "internal"):
    RUN_ERRORS.labels(_kind)
for _decision in ("approve", "edit", "reject"):
    APPROVAL_DECISIONS.labels(_decision)
for _result in ("hit", "miss"):
    MEMORY_RECALLS.labels(_result)
    WEB_SEARCH_CACHE.labels(_result)
for _limit in ("user", "ip", "login", "budget", "thread_busy"):
    LIMITED.labels(_limit)
for _kind in ("summary", "idle", "remember"):
    for _outcome in ("ok", "failed"):
        MEMORY_PUSHES.labels(_kind, _outcome)
