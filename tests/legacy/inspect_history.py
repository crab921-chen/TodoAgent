from database.checkpointer import create_checkpointer
from graph.builder import build_graph


THREAD_ID = "default-thread"


def main() -> None:
    config = {
        "configurable": {
            "thread_id": THREAD_ID,
        }
    }

    with create_checkpointer() as checkpointer:
        graph = build_graph(checkpointer)

        history = list(graph.get_state_history(config))

        print(f"TOTAL CHECKPOINTS: {len(history)}")

        for index, snapshot in enumerate(history, start=1):
            print("\n" + "=" * 60)

            print(f"SNAPSHOT: {index}")
            print(f"STEP: {snapshot.metadata.get('step')}")
            print(f"SOURCE: {snapshot.metadata.get('source')}")
            print(f"NEXT: {snapshot.next}")

            print(
                "CHECKPOINT ID:",
                snapshot.config["configurable"].get("checkpoint_id"),
            )

            messages = snapshot.values.get("messages", [])

            print(f"MESSAGE COUNT: {len(messages)}")

            if messages:
                last_message = messages[-1]

                print(
                    f"LAST MESSAGE TYPE: "
                    f"{type(last_message).__name__}"
                )

                print(
                    f"LAST MESSAGE CONTENT: "
                    f"{repr(last_message.content)}"
                )


if __name__ == "__main__":
    main()
