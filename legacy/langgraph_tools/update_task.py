import json
from typing import Optional

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from database.db import update_task
from legacy.langgraph_app.context import TodoContext


@tool
def update_task_tool(
    task_id: int,
    runtime: ToolRuntime[TodoContext],
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
) -> str:
    """Update a task belonging to the authenticated user.

    Use this tool when the user wants to modify, rename, edit,
    complete, reopen, or change an existing task.

    Args:
        task_id: The unique integer ID of the task to update.
        title: The new task title. Use null when the title should not change.
        description: The new description. Use null when the description should not change.
        status: The new task status. Use null when the status should not change.
    """

    task = update_task(
        user_id=runtime.context.user_id,
        task_id=task_id,
        title=title,
        description=description,
        status=status,
    )

    return json.dumps(task)
