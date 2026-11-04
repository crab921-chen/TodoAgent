
import json
from typing import Optional

from langchain_core.tools import tool
from langchain.tools import ToolRuntime

from database.db import add_task
from legacy.langgraph_app.context import TodoContext


@tool
def add_task_tool(
    title: str,
    runtime: ToolRuntime[TodoContext],
    description: Optional[str] = None,
) -> str:
    """Add a new task to the authenticated user's todo list.

    Use this tool when the user wants to create, add, remember,
    or save a new todo task.

    Args:
        title: A short, clear title describing the task.
        description: Optional additional details about the task.
    """

    task = add_task(
        user_id=runtime.context.user_id,
        title=title,
        description=description,
    )

    return json.dumps(task)
