from typing import Any

from langchain.agents import AgentState as _AgentState

# create_agent's state: the messages, plus a few internal keys its middleware uses.
AgentState = _AgentState[Any]
