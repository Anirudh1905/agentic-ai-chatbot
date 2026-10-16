"""The `remember` tool: the agent saves something to long-term memory when the user asks it
to ("remember that I'm vegetarian"). It goes through the same push as the UI's "Remember
this" button (memory/long_term/writer.py), in the background."""

import logging

from langchain.tools import ToolRuntime
from langchain_core.tools import BaseTool, tool

from chatbot.config import RunContext
from chatbot.memory.long_term import MemoryWriter

logger = logging.getLogger(__name__)


def build_remember_tool(writer: MemoryWriter) -> BaseTool:
    @tool
    async def remember(what: str, runtime: ToolRuntime[RunContext]) -> str:
        """Save something to the user's long-term memory, so you know it in their other
        conversations. Use it only when the user asks you to remember something ("remember
        that...", "don't forget...", "keep in mind..."). `what` is what to remember, in one
        sentence about the user, in their words."""
        if runtime.store is None or runtime.context is None:
            return "Memory isn't available, so I can't save that."
        writer.remember(runtime.store, runtime.context.user_id, what)
        logger.info("saving a memory the user asked for")
        return "Saved: it will be in your long-term memory shortly."

    return remember
