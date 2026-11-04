from dotenv import load_dotenv
from langchain_core.messages import SystemMessage
from langchain_groq import ChatGroq

from .state import AgentState
from legacy.langgraph_prompts.system_prompt import SYSTEM_PROMPT
from legacy.langgraph_tools.add_task import add_task_tool

from legacy.langgraph_tools.view_tasks import view_tasks_tool
from legacy.langgraph_tools.search_tasks import search_tasks_tool
from legacy.langgraph_tools.update_task import update_task_tool
from legacy.langgraph_tools.delete_task import delete_task_tool
from ops.runtime import ConfigurationError, load_config


load_dotenv()

try:
    runtime_config = load_config(require_secret=True)
except ConfigurationError as error:
    raise ValueError(str(error)) from error


llm = ChatGroq(
    model=runtime_config.model_name,
    temperature=runtime_config.model_temperature,
    api_key=runtime_config.groq_api_key,
)


tools = [
    add_task_tool,
    view_tasks_tool,
    search_tasks_tool,
    update_task_tool,
    delete_task_tool,
]


llm_with_tools = llm.bind_tools(tools)


def agent_node(state: AgentState) -> dict:
    """Call the LLM with system instructions and conversation messages."""

    messages_for_llm = [
        SystemMessage(content=SYSTEM_PROMPT),
        *state["messages"],
    ]

    response = llm_with_tools.invoke(messages_for_llm)

    return {
        "messages": [response]
    }
