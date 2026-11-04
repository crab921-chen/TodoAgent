from dataclasses import dataclass


@dataclass(frozen=True)
class TodoContext:
    """Trusted runtime context available during graph execution."""

    user_id: str
