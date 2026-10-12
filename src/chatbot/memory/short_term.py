"""Keep a thread within the context window by folding older turns into a running summary."""

import json
from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.messages.utils import count_tokens_approximately

from chatbot.prompts import load_prompt

# Tool results are mostly retrieved text the answer already used; the summarizer needs the gist.
TOOL_RESULT_CHARS = 1500


def split_for_summary(messages: Sequence[AnyMessage], max_tokens: int, keep_turns: int) -> int:
    """Index where the verbatim tail starts. Messages before it get summarized; 0 means none.

    The tail always starts at a user message, so a tool call is never separated from its
    result. It keeps the last `keep_turns` turns, minus older ones if they alone would take
    more than half the budget, so the next few turns don't trigger another summary straight away.
    """
    if count_tokens_approximately(messages) <= max_tokens:
        return 0
    # Possible tail starts. The first turn can't be one: summarizing must drop something.
    starts = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)][1:]
    if not starts:
        return 0
    candidates = starts[-keep_turns:]
    for start in candidates:
        if count_tokens_approximately(messages[start:]) <= max_tokens // 2:
            return start
    # Even the current turn alone is large; keep it whole anyway.
    return candidates[-1]


def render_transcript(messages: Sequence[AnyMessage]) -> str:
    lines = []
    for m in messages:
        if isinstance(m, HumanMessage):
            lines.append(f"User: {m.text}")
        elif isinstance(m, AIMessage):
            for call in m.tool_calls:
                lines.append(f"Assistant called {call['name']}({json.dumps(call['args'])})")
            if m.text:
                lines.append(f"Assistant: {m.text}")
        elif isinstance(m, ToolMessage):
            text: str = m.text
            if len(text) > TOOL_RESULT_CHARS:
                text = text[:TOOL_RESULT_CHARS] + " […]"
            lines.append(f"Tool result ({m.name}): {text}")
    return "\n\n".join(lines)


async def summarize(model: BaseChatModel, previous: str, messages: Sequence[AnyMessage]) -> str:
    """Fold `messages` into the previous summary and return the new one."""
    request = (
        f"<previous_summary>\n{previous or '(none)'}\n</previous_summary>\n\n"
        f"<transcript>\n{render_transcript(messages)}\n</transcript>"
    )
    response = await model.ainvoke([SystemMessage(load_prompt("summarize")), HumanMessage(request)])
    return response.text.strip()


def close_dangling_tool_calls(messages: Sequence[AnyMessage]) -> list[AnyMessage]:
    """Add a placeholder result after any tool call that never got one.

    That happens when a run dies mid tool call and the user sends a new message instead of
    resuming. The API rejects a tool call that isn't followed by its result.
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
