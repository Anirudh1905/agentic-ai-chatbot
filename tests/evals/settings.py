"""The settings evals run with: every setting that affects answers at its default in config.py.

A local `.env` often changes some (CONTEXT_MAX_TOKENS=1000 to watch summaries happen), and the
scores have to be comparable between machines and with `main`. So only where to reach the model
and the database come from the environment.
"""

from chatbot.config import Settings, get_settings

FROM_ENV = (
    "openai_api_key",
    "openai_base_url",
    "database_url",
    "reranker_cache_dir",
    "langfuse_public_key",
    "langfuse_secret_key",
    "langfuse_host",
    "log_level",
)


def eval_settings() -> Settings:
    env = get_settings()
    # `.env` is skipped here, but exported variables are still read: refuse any that differ from
    # the defaults (the same value exported, as `uv run` or a shell might, is fine).
    settings = Settings(_env_file=None, **{name: getattr(env, name) for name in FROM_ENV})  # type: ignore[call-arg]
    changed = [
        name.upper()
        for name, field in Settings.model_fields.items()
        if name not in FROM_ENV and getattr(settings, name) != field.get_default()
    ]
    if changed:
        raise SystemExit(f"Evals run with the defaults in config.py; unset {', '.join(changed)}")
    return settings
