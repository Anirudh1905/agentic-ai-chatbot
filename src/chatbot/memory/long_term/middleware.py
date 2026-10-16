"""LongTermMemory: recalls the user's memories into a thread, again only when they change."""

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from chatbot.config import RunContext
from chatbot.memory.long_term.store import recall, version

logger = logging.getLogger(__name__)


class MemoryState(AgentState[Any]):
    # The memories closest to the user's message when they were last recalled; "" if none.
    recalled_memories: NotRequired[str]
    # The user's memory version (store.version) they were recalled at.
    recalled_at_version: NotRequired[str | None]


class LongTermMemory(AgentMiddleware[MemoryState, RunContext]):
    """Recall what the bot knows about the user that's closest to their message: on a thread's
    first turn, and again only when their memories have changed since (a key lookup per turn).
    Between changes the system prompt stays the same, so the model's prompt cache keeps
    working, and there's no embedding call or vector search."""

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
        if "recalled_memories" in state and state.get("recalled_at_version") == current:
            return None  # nothing changed since this thread last recalled
        memories = await recall(runtime.store, user_id, said[-1].text, self.limit)
        if memories:
            logger.info("recalled %d long-term memories", memories.count("\n") + 1)
        else:
            logger.info("no long-term memories yet")
        return {"recalled_memories": memories, "recalled_at_version": current}
