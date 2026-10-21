"""Long-term memory: what the bot knows about a user across their threads.

- `facts.py`: what the memory model extracts and decides, the checks every fact passes, and
  `apply_changes`, which applies its decisions
- `store.py`: where memories live: reading them, and writing one
- `writer.py`: `MemoryWriter`, which pushes threads' turns in, in the background
- `middleware.py`: `LongTermMemory`, which recalls memories into a thread

Short-term memory is the thread itself: its messages and running summary, in the checkpointer
(`short_term.py`). Long-term memory is small memories per user, one fact each, in LangGraph's
store at `("memories", user_id)`, with a vector index on their content.

A thread's turns are pushed into long-term memory four ways:
- **Token breach:** when the thread passes CONTEXT_MAX_TOKENS, the summarization step hands
  the turns it's folding away to `MemoryWriter.schedule`.
- **TTL:** when a thread has been idle for MEMORY_THREAD_TTL_MINUTES, a background loop
  (`MemoryWriter.start`: in the worker, or in the API with BACKGROUND_JOBS) pushes its turns
  that haven't been pushed yet, or with a queue enqueues them.
- **Manually:** the user presses "Remember this" on a message, or asks the agent, which calls
  its `remember` tool (`tools/memory.py`); both go to `MemoryWriter.remember`.

A per-thread cursor at `("memory_cursor", user_id)` records the last message pushed, so the
first two never push the same turns twice. Each push:
  1. The memory model reads the turns (each labelled User or Assistant; the replies are only
     context, unless the user asked to remember one) and lists the facts the user stated,
     each tagged with a scope (about the user, or about something they're dealing with), a
     topic, and for temporary facts the date they stop being true (the store's TTL then
     hides and removes them).
  2. Facts with redaction placeholders or claims of status, discounts or permissions are
     dropped in code, whatever the model said.
  3. For each fact, the closest existing memories of that user (any topic) are found by
     vector search, and the model decides what changes: add the fact, update the memories it
     corrects, delete the ones it makes obsolete (one fact can make several obsolete).
  One push per user at a time, across processes too (a Postgres advisory lock), and at
  most MEMORY_WRITE_CONCURRENCY at once per process, since each holds a pooled connection.
- **Read** every turn: `LongTermMemory` searches them with the latest message and adds the
  closest ones the thread hasn't recalled yet to its state, where the system prompt picks
  them up. When the user's memories change (a per-user version at
  `("memory_version", user_id)`), the thread recalls them afresh.

Everything is pushed from messages the API and PIIMiddleware have already redacted
(Phase 5), so no raw PII reaches the store.
"""

from chatbot.memory.long_term.middleware import LongTermMemory
from chatbot.memory.long_term.store import (
    cursor_namespace,
    delete,
    forget_user,
    list_memories,
    namespace,
    recall,
)
from chatbot.memory.long_term.writer import MemoryWriter, wait_for_memory_writes

__all__ = [
    "LongTermMemory",
    "MemoryWriter",
    "cursor_namespace",
    "delete",
    "forget_user",
    "list_memories",
    "namespace",
    "recall",
    "wait_for_memory_writes",
]
