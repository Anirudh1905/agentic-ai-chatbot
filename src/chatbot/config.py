from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]

# Secrets can also come from files here, one per setting, named like it (`jwt_secret`,
# `openai_api_key`): Docker and Kubernetes secrets are mounted this way. The environment wins
# over the files, and the files over `.env`.
SECRETS_DIR = Path("/run/secrets")
# HS256 is only as strong as its key: at least 32 bytes (RFC 7518, section 3.2).
MIN_JWT_SECRET_CHARS = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        secrets_dir=SECRETS_DIR if SECRETS_DIR.is_dir() else None,
    )

    openai_api_key: SecretStr | None = None
    # Unset means api.openai.com; set it to route through an OpenAI-compatible gateway.
    openai_base_url: str | None = None
    # gpt-5.4 over gpt-5-mini: better answers, and faster through the gateway (1.5 s vs 2.4 s
    # median on a routing probe), at roughly 10x the token price. gpt-5-mini still works.
    model: str = "gpt-5.4"
    # Only sent to reasoning models (gpt-5.x, o-series). Below "low",
    # models answered terse questions like "president of USA" from stale memory instead of
    # searching (gpt-5-mini at "minimal": 9/16 on a routing probe; gpt-5.4 at "none": 15/16).
    reasoning_effort: ReasoningEffort | None = "low"
    max_tokens: int | None = None
    # Per model call (the reply, a summary, a memory push). The gateway sometimes stalls gpt-5
    # calls until the timeout; this fails them sooner so the retry can go through (one summary
    # hung for 126 s at 120).
    request_timeout: float = 30.0

    # When a call to MODEL fails, it goes to this one instead, on the same endpoint
    # (ModelFallbackMiddleware, Phase 9). Memory pushes fall back to it too. Empty turns
    # fallbacks off.
    fallback_model: str = "gpt-4.1"
    # Retries of a model call that failed with an outage (timeouts, connection errors, 429s,
    # 5xx), before the fallback (ModelRetryMiddleware). One: a stalled call takes
    # REQUEST_TIMEOUT each time, and the fallback answers sooner than a third try.
    model_max_retries: int = 1
    # Circuit breakers (reliability/breaker.py), per model, the embeddings and web search,
    # shared in Redis by every API pod and the worker: this many failures within the window
    # open one for the cooldown; then one call probes, and its success closes it.
    breaker_failures: int = 5
    breaker_window_seconds: int = 60
    breaker_cooldown_seconds: int = 30

    # One run at a time per thread (Phase 9, api/leases.py): a run's lease on its thread lasts
    # this long and is renewed every third of it, so a pod that dies frees its threads this
    # long after.
    thread_lease_seconds: float = 60.0

    # Cap on model calls per request (each is followed by a round of tool calls), so a
    # confused model can't loop forever.
    max_model_calls: int = 6

    # Cheap model for summarizing old turns. Same endpoint as `model`. gpt-4.1-mini over
    # gpt-5-nano: with 8 summaries at once, gpt-5-nano took 4-70 s (3 hit the timeout) and
    # gpt-4.1-mini 4-11 s; both kept the user's stated facts with prompts/summarize.md.
    summary_model: str = "gpt-4.1-mini"
    # Scores answers in the evals (tests/evals). Not the chat model, so it doesn't grade its own
    # answers. Changing it means re-running `make eval-calibrate`, and resets the baseline.
    judge_model: str = "gpt-4.1"
    # Once a thread's messages pass this many tokens (local estimate), older messages are
    # folded into a running summary, keeping the most recent half verbatim. Well under the
    # model's window: every turn resends the history. Each summary also updates the user's
    # long-term memory.
    context_max_tokens: int = 10_000
    # The retention job keeps only the latest checkpoint of threads idle this many days.
    checkpoint_retention_days: int = 30

    # Where the services are (Postgres, Redis, Langfuse) comes from .env or the environment,
    # never a default here, so nothing silently talks to a localhost (see .env.example).
    database_url: str | None = None

    embedding_model: str = "text-embedding-3-small"
    # Must match the vector column in migrations/versions/0001_chunks.py.
    embedding_dimensions: int = 1536
    chunk_size_tokens: int = 600
    chunk_overlap_tokens: int = 80
    # Candidates fetched from each of vector and full-text search before fusion.
    retrieval_candidates: int = 30
    retrieval_top_k: int = 6
    # Cross-encoder that reorders the fused results (fastembed model name). Empty disables it.
    # Measured on the golden set: top-1 accuracy 96% -> 100%, about +0.5 s per search on CPU.
    reranker_model: str | None = "BAAI/bge-reranker-v2-m3-int8"
    # How many fused results the reranker scores. Latency grows quickly past ~10.
    rerank_candidates: int = 8
    reranker_cache_dir: Path = Path.home() / ".cache" / "fastembed"

    # DuckDuckGo via the ddgs package; no API key needed.
    web_search_enabled: bool = True
    web_search_max_results: int = 5
    web_search_timeout: float = 15.0

    # Redact emails, card numbers, IPs and phone numbers: from user messages before the agent
    # runs, and from tool results and replies inside it (guardrails/pii.py).
    pii_enabled: bool = True
    # Emails at these domains are never redacted: Acme's own contact addresses must reach
    # customers. JSON list in .env.
    pii_allowed_email_domains: list[str] = ["acme-outfitters.example"]

    # Long-term memory (memory/long_term/): a thread's turns are pushed into small per-user
    # memories at its token breach, when it's been idle for MEMORY_THREAD_TTL_MINUTES, or when
    # the user presses "Remember this"; every turn recalls the closest memories.
    memory_enabled: bool = True
    # A thread's turns not yet pushed are pushed once it's been idle this long.
    memory_thread_ttl_minutes: float = 30
    # How often to look for threads whose TTL has run out.
    memory_ttl_check_every_minutes: float = 1
    # Lists the facts in a thread's turns and decides what each one changes. gpt-5-mini over
    # gpt-5-nano: on a 24-run memory spike, nano left outdated or forbidden facts behind (32/36
    # removed vs 36/36) and stored other people's details and health facts.
    memory_model: str = "gpt-5-mini"
    # Long-term memories added to each turn, closest to the user's latest message.
    memory_recall_limit: int = 10
    # Long-term memory pushes running at once per API process. Each holds a pooled connection
    # (for its per-user lock) while it waits on the model, so keep it well under the pool size.
    memory_write_concurrency: int = 2

    # Whether this process runs the background loops: the idle-thread memory loop and the
    # memory store's TTL sweeper. On for `make dev`; in Kubernetes the worker runs them and the
    # API pods set it to false, so N replicas don't run the same scans N times (Phase 9).
    background_jobs: bool = True

    # The queue memory pushes go through (chatbot/jobs, Phase 9): Amazon SQS, or ElasticMQ
    # standing in for it (SQS_ENDPOINT_URL). Unset SQS_QUEUE_URL means no queue: pushes run in
    # the process that triggered them, as before Phase 9. A FIFO queue, with a redrive policy to
    # the dead-letter queue.
    sqs_queue_url: str | None = None
    sqs_dead_letter_queue_url: str | None = None
    sqs_endpoint_url: str | None = None
    sqs_region: str = "us-east-1"
    # Only for ElasticMQ, which accepts any; on AWS leave them unset and use the pod's IAM role.
    sqs_access_key_id: str | None = None
    sqs_secret_access_key: SecretStr | None = None
    # Where the worker (python -m chatbot.worker) serves its Prometheus metrics.
    worker_metrics_port: int = 9100
    # With a port, the API serves its metrics there instead of on /metrics, which then answers
    # 404: the public port (and so the gateway) never exposes them. Set in Kubernetes; unset for
    # make dev, where Prometheus scrapes /metrics.
    metrics_port: int | None = None

    # Human approval before risky tool calls (see agent/approvals.py).
    hitl_enabled: bool = True
    # Web searches mentioning any of these (whole words, any case) wait for approval, since the
    # query goes to a third-party search engine. So do order numbers (emails and phones are
    # already redacted, Phase 5).
    hitl_sensitive_terms: list[str] = [
        "confidential",
        "internal",
        "codename",
        "Project Falcon",
        "salary",
        "payroll",
        "password",
    ]

    # Accounts log in at POST /auth/login (api/routes/auth.py) for a token signed with JWT_SECRET
    # (HS256); every request needs it as `Authorization: Bearer <JWT>`. Without a secret,
    # every request is refused. At least 32 characters: `openssl rand -hex 32`.
    jwt_secret: SecretStr | None = None
    # How long a token lasts; then the user logs in again.
    jwt_ttl_minutes: int = 60

    # Limits (accounts/limits.py), counted in Redis so every API process shares them. In the
    # `limits` library's notation. 0 for DAILY_TOKEN_BUDGET turns the budget off.
    rate_limits_enabled: bool = True
    redis_url: str | None = None
    # Against abuse, not cost (the token budget is for cost): the UI makes about three reads per
    # page run (threads, messages, memories), so these leave room for normal use.
    rate_limit_per_user: str = "120/minute"
    rate_limit_per_ip: str = "300/minute"
    # Register and log-in attempts per IP: tighter, against password guessing.
    rate_limit_login: str = "10/minute"
    # Model tokens (input + output) a user's runs may use per UTC day.
    daily_token_budget: int = 500_000

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str | None = None

    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _empty_means_unset(cls, data: Any) -> Any:
        # `OPENAI_BASE_URL=` in .env should mean "not set", not an empty URL. RERANKER_MODEL is
        # the exception: empty there explicitly turns reranking off.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != "" or k.lower() == "reranker_model"}
        return data

    @model_validator(mode="after")
    def _strong_jwt_secret(self) -> "Settings":
        if self.jwt_secret and len(self.jwt_secret.get_secret_value()) < MIN_JWT_SECRET_CHARS:
            raise ValueError(
                f"JWT_SECRET must be at least {MIN_JWT_SECRET_CHARS} characters "
                "(generate one with `openssl rand -hex 32`)"
            )
        return self

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key and self.langfuse_host)

    def require(self, name: str) -> str:
        """A setting that has no default and must be set, e.g. `require("database_url")`."""
        value = getattr(self, name)
        if not value:
            raise RuntimeError(f"{name.upper()} isn't set: add it to .env (see .env.example)")
        return str(value)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@dataclass(frozen=True)
class ModelCredentials:
    """A user's own OpenAI-compatible endpoint and key, for their runs' model calls. Checked at
    login and sent by the UI with each request; never stored on the server."""

    base_url: str
    api_key: SecretStr


@dataclass(frozen=True)
class RunContext:
    """Who a run is for: passed as LangGraph's runtime context (`context=` on every invoke and
    stream) and read by middleware and tools as `runtime.context`. The thread is in the run's
    config, where the checkpointer needs it. With `credentials`, the agent's model calls use
    the user's endpoint and key instead of the server's (agent/middleware.py)."""

    user_id: str
    credentials: ModelCredentials | None = None
