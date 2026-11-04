import json

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from database.db import get_all_tasks
from legacy.langgraph_app.context import TodoContext


@tool
def view_tasks_tool(
    runtime: ToolRuntime[TodoContext],
) -> str:
    """View all tasks belonging to the authenticated user.

    Use this tool when the user wants to see, list, display,
    or view all of their todo tasks.
    """

    tasks = get_all_tasks(
        user_id=runtime.context.user_id,
    )

    return json.dumps(tasks)
