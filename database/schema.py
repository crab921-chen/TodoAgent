"""Database initialization entry point backed by versioned migrations."""

from __future__ import annotations

import os
from pathlib import Path

from database.migrations import apply_migrations


DATABASE_PATH = Path(
    os.getenv("TODO_DATABASE_PATH", str(Path(__file__).resolve().parent / "todo.db"))
)


def initialize_database(database_path: str | Path | None = None) -> tuple[str, ...]:
    """Initialize or upgrade the database and return newly applied versions."""

    return apply_migrations(database_path or DATABASE_PATH)


if __name__ == "__main__":
    applied = initialize_database()
    print(f"Database initialized successfully at: {DATABASE_PATH}")
    print(f"Applied migrations: {', '.join(applied) or 'none'}")
