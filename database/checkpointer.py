import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from langgraph.checkpoint.sqlite import SqliteSaver


CHECKPOINT_DATABASE_PATH = Path(
    os.getenv(
        "TODO_CHECKPOINT_DATABASE_PATH",
        str(Path(__file__).resolve().parent / "checkpoints.db"),
    )
)


@contextmanager
def create_checkpointer() -> Iterator[SqliteSaver]:
    """Provide a persistent SQLite-backed LangGraph checkpointer."""

    CHECKPOINT_DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        CHECKPOINT_DATABASE_PATH,
        timeout=30,
        check_same_thread=False,
    )
    connection.execute("PRAGMA busy_timeout = 5000")

    try:
        yield SqliteSaver(connection)

    finally:
        connection.close()
