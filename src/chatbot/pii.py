"""Redact PII with LangChain's redaction rules.

"my email is jane@x.com" becomes "my email is [REDACTED_EMAIL]". Three places use it:

- **User messages**, redacted by the API before the agent is invoked (`api/runs.py`), so the
  raw value never reaches the model, any checkpoint, traces, logs or the thread title.
- **Tool results and the model's replies**, redacted by `PIIMiddleware` in the agent
  (`output_middleware`), so third-party PII from documents or web pages isn't repeated.
- **The streamed reply**, redacted as tokens arrive (`StreamRedactor`, used by
  `api/streaming.py`). The middleware only rewrites the stored reply once the model has
  finished. Its own stream redaction needs LangGraph's experimental v3 streaming, where it
  currently drops tool calls from streamed model output.

Emails at allowed domains (`PII_ALLOWED_EMAIL_DOMAINS`, Acme's own) are never redacted, so
the bot can still tell customers how to reach support.
"""

import logging
import re
from typing import Any

from langchain.agents.middleware import AgentState, PIIMatch, PIIMiddleware, RedactionRule
from langchain.agents.middleware.pii import detect_email as builtin_detect_email
from langgraph.runtime import Runtime

from chatbot.config import get_settings

logger = logging.getLogger(__name__)

# No built-in phone detector. 8+ digits with optional spaces, dots, dashes and brackets.
PHONE = re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)")
# Dates look like that too ("arrived on 2026-10-16" became "arrived on [REDACTED_PHONE]").
DATE = re.compile(r"\d{4}-\d{2}-\d{2}|\d{2}[.-]\d{2}[.-]\d{4}")


def detect_phone(content: str) -> list[PIIMatch]:
    return [
        PIIMatch(type="phone", value=m.group(), start=m.start(), end=m.end())
        for m in PHONE.finditer(content)
        if not DATE.fullmatch(m.group())
    ]


def detect_email(content: str) -> list[PIIMatch]:
    """LangChain's email detector, minus addresses at allowed domains."""
    allowed = {d.lower() for d in get_settings().pii_allowed_email_domains}
    return [
        m
        for m in builtin_detect_email(content)
        if m["value"].rsplit("@", 1)[-1].lower() not in allowed
    ]


# Card numbers before phones: a card number also looks like a long phone number.
RULES = [
    RedactionRule("email", detector=detect_email).resolve(),
    RedactionRule("credit_card").resolve(),
    RedactionRule("ip").resolve(),
    RedactionRule("phone", detector=detect_phone).resolve(),
]


def redact_pii(text: str) -> tuple[str, list[str]]:
    """The redacted text, and the PII types found (for logging; never log the values)."""
    found: list[str] = []
    for rule in RULES:
        text, matches = rule.apply(text)
        found += [m["type"] for m in matches]
    return text, found


class LoggedPIIMiddleware(PIIMiddleware):
    """PIIMiddleware that logs each redaction (the type only, never the value)."""

    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        update = await super().abefore_model(state, runtime)
        if update:
            logger.info("redacted PII from a tool result: %s", self.pii_type)
        return update

    async def aafter_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        update = await super().aafter_model(state, runtime)
        if update:
            logger.info("redacted PII from the reply: %s", self.pii_type)
        return update


def output_middleware() -> list[PIIMiddleware]:
    """Redact tool results before the model reads them, and the stored reply after it."""
    return [
        LoggedPIIMiddleware(
            r.pii_type,
            strategy=r.strategy,
            detector=r.detector,
            # User messages are already redacted by the API, before the agent runs.
            apply_to_input=False,
            apply_to_tool_results=True,
            apply_to_output=True,
        )
        for r in RULES
    ]


class StreamRedactor:
    """Redacts PII in streamed text that arrives in arbitrary pieces.

    Holds back the last `LOOKBACK` characters so a value split across tokens ("jane@ex",
    "ample.com") is seen whole before anything is released, and never releases part of a
    match. Call `feed` per token and `flush` at the end of each model call.
    """

    # Longer than any value the detectors match (emails are typically under 100 chars).
    LOOKBACK = 128

    def __init__(self) -> None:
        self.pending = ""

    def feed(self, token: str) -> str:
        self.pending += token
        cut = len(self.pending) - self.LOOKBACK
        if cut <= 0:
            return ""
        # Don't cut through a match: move the cut back to where it starts, until it's clear
        # of every match.
        spans = [(m["start"], m["end"]) for rule in RULES for m in rule.detector(self.pending)]
        moved = True
        while moved:
            moved = False
            for start, end in spans:
                if start < cut < end:
                    cut, moved = start, True
        ready, self.pending = self.pending[:cut], self.pending[cut:]
        return redact_pii(ready)[0]

    def flush(self) -> str:
        ready, self.pending = self.pending, ""
        return redact_pii(ready)[0]
