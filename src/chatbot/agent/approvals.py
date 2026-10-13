"""Which tool calls wait for a human, as config for LangChain's HumanInTheLoopMiddleware."""

import re
from collections.abc import Sequence
from typing import Any

from langchain.agents.middleware import AgentState, InterruptOnConfig, ToolCallRequest
from langchain_core.messages import ToolCall
from langchain_core.tools import BaseTool
from langgraph.runtime import Runtime
from langgraph.types import StateSnapshot

# Order numbers aren't redacted (the bot needs them to help with an order), so a web search
# containing one asks first. Emails, cards, IPs and phones never get this far: they're
# redacted from the user's message before the agent runs (chatbot/pii.py).
ORDER_NUMBER = re.compile(r"\bAC-\d{4,}\b", re.IGNORECASE)

# Tools that never need approval: they only read Acme's own data.
AUTO_APPROVED = {"search_knowledge_base"}


def sensitive_reason(query: str, terms: Sequence[str]) -> str | None:
    """Why a web query could leak internal information or an order number, or None."""
    for term in terms:
        if re.search(rf"\b{re.escape(term)}\b", query, re.IGNORECASE):
            return f'The query mentions "{term}", which may be internal information.'
    if ORDER_NUMBER.search(query):
        return "The query contains an order number."
    return None


def interrupt_on(
    tools: Sequence[BaseTool], sensitive_terms: Sequence[str]
) -> dict[str, bool | InterruptOnConfig]:
    """The middleware's `interrupt_on` for these tools.

    - search_knowledge_base runs without asking.
    - web_search asks only when the query looks sensitive, since it goes to a third-party
      search engine. The reason is shown to the reviewer as the description.
    - Any other tool always asks, so a new tool with side effects is gated until someone
      decides otherwise. (The middleware's own default is to auto-approve unlisted tools.)
    """

    def query_of(call: ToolCall) -> str:
        return str(call["args"].get("query", ""))

    def is_sensitive(request: ToolCallRequest) -> bool:
        return sensitive_reason(query_of(request.tool_call), sensitive_terms) is not None

    def describe(tool_call: ToolCall, state: AgentState[Any], runtime: Runtime[Any]) -> str:
        return sensitive_reason(query_of(tool_call), sensitive_terms) or "Needs approval."

    config: dict[str, bool | InterruptOnConfig] = {}
    for tool in tools:
        if tool.name in AUTO_APPROVED:
            config[tool.name] = False
        elif tool.name == "web_search":
            config[tool.name] = InterruptOnConfig(
                allowed_decisions=["approve", "edit", "reject"],
                when=is_sensitive,
                description=describe,
            )
        else:
            config[tool.name] = InterruptOnConfig(
                allowed_decisions=["approve", "edit", "reject"],
                description=f"{tool.name} always needs approval.",
            )
    return config


def pending_request(state: StateSnapshot) -> dict[str, Any] | None:
    """The approval request a paused thread is waiting on, or None."""
    for pending in state.interrupts:
        if isinstance(pending.value, dict) and "action_requests" in pending.value:
            return pending.value
    return None
