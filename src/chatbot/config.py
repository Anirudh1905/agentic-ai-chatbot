from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["none", "minimal", "low", "medium", "high"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    # Per model call the user waits on (the reply, a summary). A stuck gateway call fails after
    # this instead of holding the reply for minutes (one summary hung for 126 s at 120).
    request_timeout: float = 30.0

    # Cap on model calls per request (each is followed by a round of tool calls), so a
    # confused model can't loop forever.
    max_model_calls: int = 6

    # Cheap model for summarizing old turns. Same endpoint as `model`. gpt-4.1-mini over
    # gpt-5-nano: with 8 summaries at once, gpt-5-nano took 4-70 s (3 hit the timeout) and
    # gpt-4.1-mini 4-11 s; both kept the user's stated facts with prompts/summarize.md.
    summary_model: str = "gpt-4.1-mini"
    # Once a thread's messages pass this many tokens (local estimate), older messages are
    # folded into a running summary, keeping the most recent half verbatim. Well under the
    # model's window: every turn resends the history. Each summary also updates the user's
    # long-term memory.
    context_max_tokens: int = 10_000
    # The retention job keeps only the latest checkpoint of threads idle this many days.
    checkpoint_retention_days: int = 30

    database_url: str = "postgresql://chatbot:chatbot@localhost:5433/chatbot"

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
    # runs, and from tool results and replies inside it (pii.py).
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

    # Human approval before risky tool calls (see agent/approvals.py).
    hitl_enabled: bool = True
    # Web searches mentioning any of these (whole words, any case) wait for approval, since the
    # query goes to a third-party search engine. Emails, phone and order numbers always do.
    hitl_sensitive_terms: list[str] = [
        "confidential",
        "internal",
        "codename",
        "Project Falcon",
        "salary",
        "payroll",
        "password",
    ]

    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "http://localhost:3000"

    log_level: str = "INFO"

    @model_validator(mode="before")
    @classmethod
    def _empty_means_unset(cls, data: Any) -> Any:
        # `OPENAI_BASE_URL=` in .env should mean "not set", not an empty URL. RERANKER_MODEL is
        # the exception: empty there explicitly turns reranking off.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v != "" or k.lower() == "reranker_model"}
        return data

    @property
    def tracing_enabled(self) -> bool:
        return bool(self.langfuse_public_key and self.langfuse_secret_key)


@lru_cache
def get_settings() -> Settings:
    return Settings()


@dataclass(frozen=True)
class RunContext:
    """Who a run is for: passed as LangGraph's runtime context (`context=` on every invoke and
    stream) and read by middleware and tools as `runtime.context`. The thread is in the run's
    config, where the checkpointer needs it."""

    user_id: str
