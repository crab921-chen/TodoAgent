import json

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from database.db import search_tasks
from legacy.langgraph_app.context import TodoContext


@tool
def search_tasks_tool(
    query: str,
    runtime: ToolRuntime[TodoContext],
) -> str:
    """Search tasks belonging to the authenticated user.

    Use this tool when the user wants to find, search for, or filter tasks
    based on a word, phrase, topic, or other text.

    Args:
        query: The text to search for in task titles or descriptions.
    """

    tasks = search_tasks(
        user_id=runtime.context.user_id,
        query=query,
    )

    return json.dumps(tasks)
