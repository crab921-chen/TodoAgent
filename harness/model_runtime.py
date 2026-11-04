"""Small model-runtime policies used by the M5 reliability tests and app."""

from __future__ import annotations

from typing import Callable, Sequence

from harness.messages import LoopMessage, ModelResponse
from harness.tool_protocol import CancellationToken


class FallbackModelInvoker:
    """Use a fallback model when the primary adapter fails before cancellation."""

    def __init__(
        self,
        primary,
        fallback,
        *,
        on_fallback: Callable[[Exception], None] | None = None,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self.on_fallback = on_fallback

    @property
    def model_name(self) -> str:
        primary_name = getattr(self.primary, "model_name", type(self.primary).__name__)
        fallback_name = getattr(self.fallback, "model_name", type(self.fallback).__name__)
        return f"{primary_name}|fallback:{fallback_name}"

    def complete(
        self,
        messages: Sequence[LoopMessage],
        tools,
        cancellation_token: CancellationToken,
    ) -> ModelResponse:
        try:
            return self.primary.complete(messages, tools, cancellation_token)
        except Exception as error:
            if cancellation_token.cancelled:
                raise
            if self.on_fallback is not None:
                self.on_fallback(error)
            return self.fallback.complete(messages, tools, cancellation_token)


__all__ = ["FallbackModelInvoker"]
