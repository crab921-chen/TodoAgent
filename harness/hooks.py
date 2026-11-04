"""Bounded observability hooks for model and tool runtime events.

Events intentionally carry identifiers and summaries rather than prompts or
complete tool arguments. Hooks must not implement business writes or alter
the result of a model/tool call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Iterable, Literal, Mapping, Protocol


logger = logging.getLogger(__name__)

EventKind = Literal[
    "model",
    "tool",
    "high_risk_confirm",
    "budget_exceeded",
    "cancellation",
]
EventPhase = Literal["before", "after", "occurred"]
_EVENT_KINDS = frozenset(
    {"model", "tool", "high_risk_confirm", "budget_exceeded", "cancellation"}
)
_EVENT_PHASES = frozenset({"before", "after", "occurred"})
_MAX_METADATA_ITEMS = 32
_MAX_METADATA_KEY_LENGTH = 64
_MAX_METADATA_VALUE_LENGTH = 500


def digest_tool_input(arguments: Mapping[str, Any]) -> str:
    """Return a stable SHA-256 digest without exposing the input payload."""

    serialized = json.dumps(
        arguments,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _sanitize_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    """Keep hook metadata small and free of nested/raw payloads."""

    sanitized: dict[str, Any] = {}
    for index, (key, value) in enumerate(metadata.items()):
        if index >= _MAX_METADATA_ITEMS:
            break
        if not isinstance(key, str) or not key:
            raise ValueError("hook metadata keys must be non-empty strings")
        if len(key) > _MAX_METADATA_KEY_LENGTH:
            raise ValueError("hook metadata keys are too long")
        if isinstance(value, (str, int, float, bool)) or value is None:
            if isinstance(value, str):
                value = value[:_MAX_METADATA_VALUE_LENGTH]
            elif isinstance(value, float) and not math.isfinite(value):
                value = None
            sanitized[key] = value
        else:
            # Lists, mappings, model objects, and arbitrary objects may carry
            # prompts or sensitive fields, so they are never retained here.
            sanitized[key] = "<redacted>"
    return sanitized


@dataclass(frozen=True)
class RuntimeEvent:
    """A bounded, request-correlated runtime lifecycle event."""

    kind: EventKind
    phase: EventPhase
    request_id: str
    user_id: str
    session_key: str | None = None
    duration_seconds: float | None = None
    tool_name: str | None = None
    tool_call_id: str | None = None
    permission_level: str | None = None
    tool_input_digest: str | None = None
    model_name: str | None = None
    token_usage: int | None = None
    ok: bool | None = None
    error_code: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in _EVENT_KINDS:
            raise ValueError(f"unsupported runtime event kind: {self.kind}")
        if self.phase not in _EVENT_PHASES:
            raise ValueError(f"unsupported runtime event phase: {self.phase}")
        if not self.request_id.strip():
            raise ValueError("request_id must not be blank")
        if not self.user_id.strip():
            raise ValueError("user_id must not be blank")
        if self.session_key is not None and not self.session_key.strip():
            raise ValueError("session_key must not be blank")
        if self.duration_seconds is not None and (
            not math.isfinite(self.duration_seconds) or self.duration_seconds < 0
        ):
            raise ValueError("duration_seconds must be finite and non-negative")
        if self.token_usage is not None and self.token_usage < 0:
            raise ValueError("token_usage must be non-negative")
        object.__setattr__(self, "metadata", _sanitize_metadata(self.metadata))


class RuntimeHook(Protocol):
    """A consumer of non-blocking runtime events."""

    def emit(self, event: RuntimeEvent) -> None:
        ...


class HookSet:
    """Thread-safe hook fan-out with runtime registration support."""

    def __init__(self, hooks: RuntimeHook | Iterable[RuntimeHook] | None = None) -> None:
        self._lock = RLock()
        if hooks is None:
            initial: tuple[RuntimeHook, ...] = ()
        elif hasattr(hooks, "emit"):
            initial = (hooks,)  # type: ignore[assignment]
        else:
            initial = tuple(hooks)
        self._hooks = initial

    def register(self, hook: RuntimeHook) -> None:
        """Register a hook once; safe while events are being emitted."""

        if not hasattr(hook, "emit"):
            raise TypeError("hook must expose emit(event)")
        with self._lock:
            if hook not in self._hooks:
                self._hooks = (*self._hooks, hook)

    def unregister(self, hook: RuntimeHook) -> bool:
        """Remove a hook and report whether it was registered."""

        with self._lock:
            if hook not in self._hooks:
                return False
            self._hooks = tuple(item for item in self._hooks if item is not hook)
            return True

    def clear(self) -> None:
        """Remove all hooks without affecting in-flight event snapshots."""

        with self._lock:
            self._hooks = ()

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            hooks = self._hooks
        for hook in hooks:
            try:
                hook.emit(event)
            except Exception:
                # Observability must not turn a successful business operation
                # into a failure, but hook failures remain visible to operators.
                logger.exception("Runtime hook failed")


class RecordingHook:
    """Thread-safe in-memory hook useful for tests and local diagnostics."""

    def __init__(self) -> None:
        self._events: list[RuntimeEvent] = []
        self._lock = RLock()

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            self._events.append(event)

    @property
    def events(self) -> tuple[RuntimeEvent, ...]:
        with self._lock:
            return tuple(self._events)

    def clear(self) -> None:
        """Clear recorded events so one test/request cannot contaminate another."""

        with self._lock:
            self._events.clear()
