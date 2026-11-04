from typing import Literal

from .state import AgentState


def route_after_agent(state: AgentState) -> Literal["tools", "__end__"]:
    """Route to the tool node when the model requests tool execution."""

    last_message = state["messages"][-1]

    if last_message.tool_calls:
        return "tools"

    return "__end__"
