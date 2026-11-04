"""Permission and confirmation policy for model-requested tool calls."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from harness.messages import ToolCall
from harness.tool_protocol import (
    ErrorCode,
    PermissionLevel,
    ToolCallContext,
    ToolResult,
    ToolSpec,
)


@dataclass(frozen=True)
class ConfirmationResult:
    """Explicit result returned by a confirmation UI or service."""

    confirmed: bool
    reason: str | None = None


ConfirmationCallback = Callable[
    [ToolCallContext, ToolCall, ToolSpec],
    bool | ConfirmationResult,
]


class ToolAuthorization(Protocol):
    """Policy boundary checked before a handler is allowed to run."""

    def authorize(
        self,
        call: ToolCall,
        spec: ToolSpec,
        context: ToolCallContext,
    ) -> ToolResult | None:
        """Return a structured denial, or None when execution is allowed."""


class PermissionPolicy:
    """Allow-list policy with explicit confirmation for high-risk tools.

    ``HIGH_RISK`` remains in the default allow-list because it is a valid
    policy level, but it can never execute without a successful confirmation
    callback. An empty explicit allow-list remains empty.
    """

    def __init__(
        self,
        *,
        allowed_levels: frozenset[PermissionLevel] | None = None,
        denied_tool_names: frozenset[str] | None = None,
        confirmation_callback: ConfirmationCallback | None = None,
    ) -> None:
        configured_levels = (
            {PermissionLevel.READ, PermissionLevel.WRITE, PermissionLevel.HIGH_RISK}
            if allowed_levels is None
            else allowed_levels
        )
        try:
            self.allowed_levels = frozenset(
                PermissionLevel(level) for level in configured_levels
            )
        except (TypeError, ValueError) as error:
            raise ValueError("allowed_levels must contain valid permission levels") from error
        self.denied_tool_names = frozenset(denied_tool_names or ())
        self.confirmation_callback = confirmation_callback

        if any(
            not isinstance(name, str) or not name.strip()
            for name in self.denied_tool_names
        ):
            raise ValueError("denied tool names must not be blank")

    def authorize(
        self,
        call: ToolCall,
        spec: ToolSpec,
        context: ToolCallContext,
    ) -> ToolResult | None:
        try:
            permission_level = PermissionLevel(spec.permission_level)
        except (TypeError, ValueError):
            return ToolResult.failure(
                ErrorCode.PERMISSION_DENIED,
                "Tool permission level is invalid.",
                details={
                    "tool": spec.name,
                    "reason": "invalid_permission_level",
                },
                request_id=context.request_id,
            )

        if spec.name in self.denied_tool_names:
            return ToolResult.failure(
                ErrorCode.PERMISSION_DENIED,
                "This tool is disabled by policy.",
                details={"tool": spec.name, "reason": "tool_denied"},
                request_id=context.request_id,
            )

        if permission_level not in self.allowed_levels:
            return ToolResult.failure(
                ErrorCode.PERMISSION_DENIED,
                f"Permission level '{permission_level}' is not allowed.",
                details={
                    "tool": spec.name,
                    "permission_level": permission_level,
                    "reason": "permission_level_denied",
                },
                request_id=context.request_id,
            )

        if permission_level is not PermissionLevel.HIGH_RISK:
            return None

        if self.confirmation_callback is None:
            return ToolResult.failure(
                ErrorCode.CONFIRMATION_REQUIRED,
                "This high-risk tool requires explicit confirmation.",
                details={
                    "tool": spec.name,
                    "tool_call_id": call.id,
                    "reason": "confirmation_callback_missing",
                },
                request_id=context.request_id,
            )

        try:
            decision = self.confirmation_callback(context, call, spec)
        except Exception as error:
            return ToolResult.failure(
                ErrorCode.CONFIRMATION_FAILED,
                "High-risk tool confirmation could not be completed.",
                details={
                    "tool": spec.name,
                    "tool_call_id": call.id,
                    "reason": "confirmation_callback_error",
                    "exception_type": type(error).__name__,
                },
                request_id=context.request_id,
            )

        if isinstance(decision, ConfirmationResult):
            confirmed = decision.confirmed
            reason = str(decision.reason or "confirmation_denied")[:200]
        elif isinstance(decision, bool):
            confirmed = decision
            reason = "confirmed" if decision else "confirmation_denied"
        else:
            return ToolResult.failure(
                ErrorCode.CONFIRMATION_FAILED,
                "Confirmation callback returned an invalid decision.",
                details={
                    "tool": spec.name,
                    "tool_call_id": call.id,
                    "reason": "invalid_confirmation_result",
                },
                request_id=context.request_id,
            )

        if not confirmed:
            return ToolResult.failure(
                ErrorCode.CONFIRMATION_DENIED,
                "High-risk tool execution was not confirmed.",
                details={
                    "tool": spec.name,
                    "tool_call_id": call.id,
                    "reason": reason,
                },
                request_id=context.request_id,
            )
        return None
