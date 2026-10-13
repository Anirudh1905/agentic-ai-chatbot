"""Agent middleware around the model call. Mostly LangChain's built-ins, configured here."""

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import date
from typing import Any

from langchain.agents.middleware import (
    AgentMiddleware,
    AgentState,
    ModelCallLimitMiddleware,
    ModelRequest,
    ModelResponse,
    SummarizationMiddleware,
    dynamic_prompt,
    hook_config,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately
from langgraph.runtime import Runtime

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


def summarization(settings: Settings, model: BaseChatModel) -> Summarization:
    limit = settings.context_max_tokens
    return Summarization(
        model,
        trigger=("tokens", limit),
        # Keeping half the budget means the next few turns don't trigger another summary.
        keep=("tokens", limit // 2),
        summary_prompt=load_prompt("summarize"),
        # Summarize everything being dropped, so the previous summary is never cut off.
        trim_tokens_to_summarize=None,
    )


@dynamic_prompt
def system_prompt(_request: ModelRequest) -> str:
    return load_prompt("system").replace("{today}", date.today().isoformat())


class ModelCallLimit(ModelCallLimitMiddleware):
    """ModelCallLimitMiddleware that logs when it stops a run. A model that keeps calling
    tools without answering is a bug worth seeing in the logs."""

    @hook_config(can_jump_to=["end"])
    async def abefore_model(
        self, state: AgentState[Any], runtime: Runtime[Any]
    ) -> dict[str, Any] | None:
        update = await super().abefore_model(state, runtime)  # type: ignore[arg-type]
        if update and update.get("jump_to") == "end":
            logger.warning("stopped the run: %s", update["messages"][0].text)
        return update


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


class ModelCallMiddleware(AgentMiddleware[AgentState[Any], Any]):
    """Repairs tool calls left without a result, and logs each model call."""

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        messages = close_dangling_tool_calls(request.messages)
        if len(messages) != len(request.messages):
            logger.warning("added placeholder results for interrupted tool calls")
            request = request.override(messages=messages)
        start = time.monotonic()
        response = await handler(request)
        if response.result and isinstance(reply := response.result[-1], AIMessage):
            log_model_call(reply, time.monotonic() - start)
        return response


def log_model_call(response: AIMessage, seconds: float) -> None:
    usage = response.usage_metadata
    if response.tool_calls:
        outcome = "tool calls: " + ", ".join(c["name"] for c in response.tool_calls)
    else:
        outcome = f"answer of {len(response.text)} chars"
    logger.info(
        "model replied in %.1fs (%s tokens in, %s out): %s",
        seconds,
        usage["input_tokens"] if usage else "?",
        usage["output_tokens"] if usage else "?",
        outcome,
    )
    if response.response_metadata.get("finish_reason") == "content_filter":
        logger.warning("response blocked by content filter")
