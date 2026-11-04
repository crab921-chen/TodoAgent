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

        state_snapshot = graph.get_state(config)

        print("VALUES:")
        print(state_snapshot.values)

        print("\nNEXT:")
        print(state_snapshot.next)

        print("\nCONFIG:")
        print(state_snapshot.config)

        print("\nMETADATA:")
        print(state_snapshot.metadata)


if __name__ == "__main__":
    main()
