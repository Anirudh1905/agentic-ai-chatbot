import logging

from langchain_core.callbacks import BaseCallbackHandler

from chatbot.config import Settings


def configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_callbacks(settings: Settings) -> list[BaseCallbackHandler]:
    if not settings.tracing_enabled:
        return []

    from langfuse import Langfuse
    from langfuse.langchain import CallbackHandler

    assert settings.langfuse_secret_key is not None
    Langfuse(
        public_key=settings.langfuse_public_key,
        secret_key=settings.langfuse_secret_key.get_secret_value(),
        host=settings.langfuse_host,
    )
    return [CallbackHandler(public_key=settings.langfuse_public_key)]
