import json

from langchain.tools import ToolRuntime
from langchain_core.tools import tool

from database.db import delete_task
from legacy.langgraph_app.context import TodoContext


@tool
def delete_task_tool(
    task_id: int,
    runtime: ToolRuntime[TodoContext],
) -> str:
    """Delete a task belonging to the authenticated user.

    Use this tool when the user wants to remove or delete a task.

    Args:
        task_id: The unique integer ID of the task to delete.
    """

    deleted = delete_task(
        user_id=runtime.context.user_id,
        task_id=task_id,
    )

    return json.dumps(
        {
            "task_id": task_id,
            "deleted": deleted,
        }
    )
