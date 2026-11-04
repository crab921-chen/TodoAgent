from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from .context import TodoContext
from .nodes import agent_node, tools
from .routing import route_after_agent
from .state import AgentState


def build_graph(checkpointer: BaseCheckpointSaver):
    """Build and compile the Todo Agent graph."""

    builder = StateGraph(
        AgentState,
        context_schema=TodoContext,
    )

    tool_node = ToolNode(tools)

    builder.add_node("agent", agent_node)
    builder.add_node("tools", tool_node)

    builder.add_edge(START, "agent")

    builder.add_conditional_edges(
        "agent",
        route_after_agent,
        {
            "tools": "tools",
            "__end__": END,
        },
    )

    builder.add_edge("tools", "agent")

    graph = builder.compile(checkpointer=checkpointer)

    return graph
