"""Cross-encoder reranking for knowledge base results.

Run `python -m chatbot.tools.rerank` to download and cache the model ahead of time.
"""

import asyncio
import logging
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Protocol

from fastembed.rerank.cross_encoder import TextCrossEncoder

from chatbot.config import Settings, get_settings
from chatbot.tools.rag import SearchHit

logger = logging.getLogger(__name__)


class Reranker(Protocol):
    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]: ...


class CrossEncoderReranker:
    """Scores (query, chunk) pairs with a local ONNX cross-encoder. The model loads on first use."""

    def __init__(self, model_name: str, cache_dir: Path) -> None:
        self.model_name = model_name
        self.cache_dir = cache_dir
        self._model: TextCrossEncoder | None = None
        self._lock = threading.Lock()

    def load(self) -> TextCrossEncoder:
        with self._lock:
            if self._model is None:
                start = time.monotonic()
                self._model = TextCrossEncoder(self.model_name, cache_dir=str(self.cache_dir))
                logger.info(
                    "loaded reranker %s in %.1fs", self.model_name, time.monotonic() - start
                )
            return self._model

    @staticmethod
    def hit_text(hit: SearchHit) -> str:
        return "\n".join(filter(None, [hit.title, hit.heading, hit.content]))

    def _score(self, query: str, texts: list[str]) -> list[float]:
        return [float(s) for s in self.load().rerank(query, texts)]

    async def rerank(self, query: str, hits: list[SearchHit]) -> list[SearchHit]:
        if not hits:
            return hits
        # CPU-bound; keep it off the event loop.
        scores = await asyncio.to_thread(self._score, query, [self.hit_text(h) for h in hits])
        ranked = sorted(zip(scores, hits, strict=True), key=lambda pair: pair[0], reverse=True)
        return [replace(hit, score=score) for score, hit in ranked]


def build_reranker(settings: Settings) -> CrossEncoderReranker | None:
    if not settings.reranker_model:
        return None
    return CrossEncoderReranker(settings.reranker_model, settings.reranker_cache_dir)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    reranker = build_reranker(get_settings())
    if reranker is None:
        print("RERANKER_MODEL is empty; reranking is disabled.")
    else:
        reranker.load()
        print(f"Reranker {reranker.model_name} is cached in {reranker.cache_dir}")
