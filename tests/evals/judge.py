"""The judge model behind DeepEval's metrics: JUDGE_MODEL through our own model client.

DeepEval is never logged in to its hosted service, and its telemetry is off: nothing about the
test cases leaves the machine except the judge's own calls to the gateway.
"""

import asyncio
import os

os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")  # before deepeval is imported

import deepeval  # noqa: E402
from deepeval.models import DeepEvalBaseLLM  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from chatbot.agent.builder import build_llm  # noqa: E402
from chatbot.config import Settings  # noqa: E402
from evals.report import DEEPEVAL_VERSION  # noqa: E402

if deepeval.__version__ != DEEPEVAL_VERSION:
    raise SystemExit(f"Evals need deepeval {DEEPEVAL_VERSION}, not {deepeval.__version__}")

CONCURRENCY = 3
ATTEMPTS = 3


class JudgeModel(DeepEvalBaseLLM):  # type: ignore[misc]
    def __init__(self, settings: Settings) -> None:
        self.llm = build_llm(settings, settings.judge_model, streaming=False)
        self.slots = asyncio.Semaphore(CONCURRENCY)
        super().__init__(settings.judge_model)

    def load_model(self) -> object:
        return self.llm

    def get_model_name(self) -> str:
        return str(self.name)

    def generate(self, prompt: str, schema: type[BaseModel] | None = None) -> object:
        if schema:
            return self.llm.with_structured_output(schema).invoke(prompt)
        return self.llm.invoke(prompt).text

    async def a_generate(self, prompt: str, schema: type[BaseModel] | None = None) -> object:
        # The gateway stalls calls now and then; one judge call failing shouldn't lose a case.
        for attempt in range(ATTEMPTS):
            try:
                async with self.slots:
                    if schema:
                        return await self.llm.with_structured_output(schema).ainvoke(prompt)
                    return (await self.llm.ainvoke(prompt)).text
            except Exception:
                if attempt == ATTEMPTS - 1:
                    raise
                await asyncio.sleep(5 * (attempt + 1))
        raise AssertionError("unreachable")
