"""The golden questions (golden.jsonl): the tools each should use, and for knowledge base
questions, the section that answers it and a reference answer written from the sample docs."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).with_name("golden.jsonl")


@dataclass(frozen=True)
class Case:
    question: str
    tools: list[str]
    expect: dict[str, Any] | None = None  # {"doc": ..., "heading": ...} for retrieval
    reference: str | None = None  # the right answer, for correctness

    @property
    def id(self) -> str:
        return self.question


def load_cases() -> list[Case]:
    return [Case(**json.loads(line)) for line in GOLDEN.read_text().splitlines() if line.strip()]
