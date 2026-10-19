"""Prometheus metrics, served at GET /metrics (Phase 8).

Prometheus scrapes them (`docker compose up` runs it, with the alert rules in
`ops/prometheus/alerts.yml`) and Grafana charts them (the "Chatbot" dashboard). Labels are
kept to small fixed sets (route templates, tool and model names, PII types), never users or
threads: those are in the traces (tracing.py).

The counts are per process. With several API processes each needs scraping (Phase 9); the
dashboards sum them.
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

TOOL_CALLS = Counter(
    "chatbot_tool_calls_total",
    "Tool calls, after retries; outcome is ok or error",
    ["tool", "outcome"],
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
    "Requests refused with 429: a rate limit (user, ip, login) or the token budget",
    ["limit"],
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
