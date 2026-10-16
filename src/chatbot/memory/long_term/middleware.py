"""LongTermMemory: recalls the user's memories into every turn."""

import logging
from typing import Any, NotRequired

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import HumanMessage
from langgraph.runtime import Runtime

from chatbot.config import RunContext
from chatbot.memory.long_term.store import recall

logger = logging.getLogger(__name__)


class MemoryState(AgentState[Any]):
    # The memories closest to the user's latest message; "" if there are none.
    recalled_memories: NotRequired[str]


class LongTermMemory(AgentMiddleware[MemoryState, RunContext]):
    """Every turn, recall what the bot knows about the user that's closest to their latest
    message."""

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
        memories = await recall(runtime.store, runtime.context.user_id, said[-1].text, self.limit)
        if memories:
            logger.info("recalled %d long-term memories", memories.count("\n") + 1)
        else:
            logger.info("no long-term memories yet")
        return {"recalled_memories": memories}
