"""LongTermMemory: recalls the user's memories into a thread as its topics come up."""

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from chatbot.config import RunContext
from chatbot.memory.long_term.store import recall, version
from chatbot.metrics import MEMORY_RECALLS

logger = logging.getLogger(__name__)


class MemoryState(AgentState[Any]):
    # The memories recalled into this thread so far, one per line; "" if none.
    recalled_memories: NotRequired[str]
    # The user's memory version (store.version) they were recalled at.
    recalled_at_version: NotRequired[str | None]


class LongTermMemory(AgentMiddleware[MemoryState, RunContext]):
    """Recall what the bot knows about the user that's closest to each of their messages.

    The memories a thread has recalled are kept in its state, and each turn only adds the
    ones its message brings up that aren't there yet. So a thread that opens on returns still
    finds "User is vegetarian" when it turns to trail meals, and the system prompt only changes
    when something new turns up, which keeps the model's prompt cache working. When the user's
    memories change (a write or delete bumps their version), the set is recalled afresh, so a
    corrected or forgotten memory doesn't linger."""

    state_schema = MemoryState

    def __init__(self, limit: int) -> None:
        super().__init__()
        self.limit = limit

    async def abefore_agent(
        self, state: MemoryState, runtime: Runtime[RunContext]
    ) -> dict[str, Any] | None:
        if runtime.store is None or runtime.context is None:
            return None
        said = [m for m in state["messages"] if isinstance(m, HumanMessage)]
        if not said:
            return None
        user_id = runtime.context.user_id
        current = await version(runtime.store, user_id)
        memories = await recall(runtime.store, user_id, said[-1].text, self.limit)
        MEMORY_RECALLS.labels("hit" if memories else "miss").inc()
        if "recalled_memories" in state and state.get("recalled_at_version") == current:
            known = state["recalled_memories"].splitlines()
            new = [line for line in memories.splitlines() if line not in known]
            if not new:
                return None  # nothing this thread hasn't seen
            logger.info("recalled %d more long-term memories", len(new))
            # Each line starts with its date, so this keeps them oldest first.
            return {"recalled_memories": "\n".join(sorted(known + new))}
        if memories:
            logger.info("recalled %d long-term memories", memories.count("\n") + 1)
        else:
            logger.info("no long-term memories yet")
        return {"recalled_memories": memories, "recalled_at_version": current}
