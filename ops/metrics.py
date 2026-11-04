"""Small local metrics collector for Harness runtime events."""

from __future__ import annotations

from collections import Counter
from threading import Lock
from typing import Any

from harness.hooks import RuntimeEvent


class RuntimeMetrics:
    """Aggregate bounded counters and durations without retaining event payloads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._requests: set[str] = set()
        self._failed_requests: set[str] = set()
        self._counters: Counter[str] = Counter()
        self._durations: Counter[str] = Counter()
        self._errors: Counter[str] = Counter()

    def emit(self, event: RuntimeEvent) -> None:
        with self._lock:
            self._requests.add(event.request_id)
            if event.ok is False and event.request_id not in self._failed_requests:
                self._failed_requests.add(event.request_id)
                self._counters["request_failure_total"] += 1
            if event.error_code:
                self._errors[str(event.error_code)] += 1
            if event.kind == "model" and event.phase == "before":
                self._counters["model_call_total"] += 1
            elif event.kind == "model" and event.phase == "after":
                self._counters["model_call_failure_total"] += int(event.ok is False)
                if event.duration_seconds is not None:
                    self._durations["model_call_duration_ms"] += int(
                        event.duration_seconds * 1000
                    )
            elif event.kind == "tool" and event.phase == "before":
                self._counters["tool_call_total"] += 1
            elif event.kind == "tool" and event.phase == "after":
                self._counters["tool_call_failure_total"] += int(event.ok is False)
                if event.duration_seconds is not None:
                    self._durations["tool_duration_ms"] += int(
                        event.duration_seconds * 1000
                    )
            elif event.kind == "budget_exceeded":
                self._counters["budget_exceeded_total"] += 1
            elif event.kind == "cancellation":
                self._counters["cancellation_total"] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a copy suitable for local diagnostics or a health endpoint."""

        with self._lock:
            result: dict[str, Any] = {
                "request_total": len(self._requests),
                **dict(self._counters),
                **dict(self._durations),
                "error_code_counts": dict(self._errors),
            }
        return result
