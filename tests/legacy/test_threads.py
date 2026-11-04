from langchain_core.messages import HumanMessage

from database.checkpointer import create_checkpointer
from graph.builder import build_graph


THREAD_A = "thread-A"
THREAD_B = "thread-B"


def create_config(thread_id: str) -> dict:
    """Create LangGraph configuration for a conversation thread."""

    return {
        "configurable": {
            "thread_id": thread_id,
        }
    }


def main() -> None:
    config_a = create_config(THREAD_A)
    config_b = create_config(THREAD_B)

    with create_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)

        graph.invoke(
            {
                "messages": [
                    HumanMessage(content="My name is Aman.")
                ]
            },
            config=config_a,
        )

        graph.invoke(
            {
                "messages": [
                    HumanMessage(content="My name is Rahul.")
                ]
            },
            config=config_b,
        )

        result_a = graph.invoke(
            {
                "messages": [
                    HumanMessage(content="What is my name?")
                ]
            },
            config=config_a,
        )

        result_b = graph.invoke(
            {
                "messages": [
                    HumanMessage(content="What is my name?")
                ]
            },
            config=config_b,
        )

        print("THREAD A:")
        print(result_a["messages"][-1].content)

        print("\nTHREAD B:")
        print(result_b["messages"][-1].content)


if __name__ == "__main__":
    main()
