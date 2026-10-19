from langchain_core.embeddings import Embeddings
from langchain_openai import OpenAIEmbeddings

from chatbot.config import Settings


def build_embeddings(settings: Settings) -> Embeddings:
    return OpenAIEmbeddings(
        model=settings.embedding_model,
        dimensions=settings.embedding_dimensions,
        openai_api_key=settings.openai_api_key,
        openai_api_base=settings.openai_base_url or None,
        # By default inputs are sent as token IDs, which some gateways reject. Send plain text.
        check_embedding_ctx_length=False,
    )
