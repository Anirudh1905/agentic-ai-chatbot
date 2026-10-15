"""Short-term memory: the current thread.

The thread's messages live in the checkpointer (Phase 3). Once they pass
CONTEXT_MAX_TOKENS, older turns are folded into a running summary by LangChain's
SummarizationMiddleware (adjusted below); the summary is also handed to long-term memory
(`long_term.py`), which updates the user's memories from it. `close_dangling_tool_calls`
repairs a thread whose run died mid tool call.
"""

import logging
import time
from collections.abc import Callable
from typing import Any

from langchain.agents.middleware import AgentState, SummarizationMiddleware
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.runtime import Runtime
from langgraph.store.base import BaseStore

from chatbot.config import Settings
from chatbot.prompts import load_prompt

logger = logging.getLogger(__name__)


# SummarizationMiddleware stores the summary as a HumanMessage marked with this source.
SUMMARY_SOURCE = "summarization"
SUMMARY_PREFIX = "Here is a summary of the conversation to date:\n\n"


def summary_text(message: AnyMessage) -> str | None:
    """The summary, if `message` is the one SummarizationMiddleware put in the thread."""
    if message.additional_kwargs.get("lc_source") != SUMMARY_SOURCE:
        return None
    return message.text.removeprefix(SUMMARY_PREFIX)


class Summarization(SummarizationMiddleware):
    """SummarizationMiddleware with three changes, each covered by a test:

    - Only at the start of a turn. The built-in runs before every model call, so a turn with
      tool calls could be summarized twice, mid-answer.
    - Cuts at a turn boundary and keeps the current turn whole. The built-in only avoids
      splitting a tool call from its result, so it could summarize away the question being
      answered.
    - If the summary model fails, the turn goes ahead with the full history instead of failing.
    """

    # Called with (store, summary) after each successful summary; long-term memory uses it.
    on_summary: Callable[[BaseStore | None, str], None] | None = None

    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        messages = state["messages"]
        if not messages or not isinstance(messages[-1], HumanMessage):
            return None
        logger.info(
            "context: %d messages, ~%d tokens", len(messages), count_tokens_approximately(messages)
        )
        start = time.monotonic()
        try:
            update = await super().abefore_model(state, runtime)
        except Exception:
            # Answering with a long history beats not answering; try again next turn.
            logger.exception("summarizing failed")
            return None
        if update:
            # The update is [remove all, summary, kept messages...].
            if self.on_summary and (summary := summary_text(update["messages"][1])):
                self.on_summary(runtime.store, summary)
            logger.info(
                "summarized older messages in %.1fs; thread now %d messages, ~%d tokens",
                time.monotonic() - start,
                len(update["messages"]) - 1,
                count_tokens_approximately(update["messages"][1:]),
            )
        return update

    def _determine_cutoff_index(self, messages: list[AnyMessage]) -> int:
        cutoff = super()._determine_cutoff_index(messages)
        # Move the cut back to where a turn starts, and never past the current one.
        turn_starts = [
            i for i, m in enumerate(messages) if isinstance(m, HumanMessage) and i <= cutoff
        ]
        current_turn = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        return min(turn_starts[-1] if turn_starts else 0, current_turn)


def summarization(
    settings: Settings,
    model: BaseChatModel,
    on_summary: Callable[[BaseStore | None, str], None] | None = None,
) -> Summarization:
    limit = settings.context_max_tokens
    middleware = Summarization(
        model,
        trigger=("tokens", limit),
        # Keeping half the budget means the next few turns don't trigger another summary.
        keep=("tokens", limit // 2),
        summary_prompt=load_prompt("summarize"),
        # Summarize everything being dropped, so the previous summary is never cut off.
        trim_tokens_to_summarize=None,
    )
    middleware.on_summary = on_summary
    return middleware


def close_dangling_tool_calls(messages: list[AnyMessage]) -> list[AnyMessage]:
    """Add a placeholder result after any tool call that never got one.

    That happens when a run dies mid tool call and the user sends a new message instead of
    resuming. The model API rejects a tool call that isn't followed by its result.
    """
    answered = {m.tool_call_id for m in messages if isinstance(m, ToolMessage)}
    out: list[AnyMessage] = []
    for m in messages:
        out.append(m)
        if isinstance(m, AIMessage):
            out.extend(
                ToolMessage(
                    "No result: this tool call was interrupted.",
                    tool_call_id=call["id"],
                    name=call["name"],
                )
                for call in m.tool_calls
                if call["id"] not in answered
            )
    return out
