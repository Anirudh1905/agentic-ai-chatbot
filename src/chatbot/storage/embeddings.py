from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from chatbot.config import Settings
from chatbot.reliability.breaker import Breaker, Breakers

# The embeddings' circuit breaker (reliability/breaker.py). There's no fallback: vectors from
# another model wouldn't match the stored ones. Searches go on with full text only (tools/rag.py).
BREAKER = "embeddings"


class GuardedEmbeddings(Embeddings):
    """Embeddings whose async calls go through a circuit breaker: while it's open they raise
    BreakerOpen at once instead of waiting on a timeout."""

    def __init__(self, inner: Embeddings, breaker: Breaker) -> None:
        self.inner = inner
        self.breaker = breaker

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self.inner.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(text)

    async def aembed_documents(self, texts: list[str]) -> list[list[float]]:
        return await self.breaker.call(lambda: self.inner.aembed_documents(texts))

    async def aembed_query(self, text: str) -> list[float]:
        return await self.breaker.call(lambda: self.inner.aembed_query(text))


def build_embeddings(settings: Settings, breakers: Breakers | None = None) -> Embeddings:
    embeddings = OpenAIEmbeddings(
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        openai_api_key=settings.openai_api_key,
        openai_api_base=settings.openai_base_url or None,
        # By default inputs are sent as token IDs, which some gateways reject. Send plain text.
        check_embedding_ctx_length=False,
        max_retries=settings.model_max_retries,
    )
    return GuardedEmbeddings(embeddings, breakers.get(BREAKER)) if breakers else embeddings
