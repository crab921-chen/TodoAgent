"""Tool registration and single-call dispatch for the Harness runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from time import monotonic
from typing import Any, Iterable, Mapping

from harness.hooks import HookSet, RuntimeEvent, RuntimeHook, digest_tool_input
from harness.messages import ToolCall
from harness.security import PermissionPolicy, ToolAuthorization
from harness.tool_protocol import (
    ErrorCode,
    PermissionLevel,
    ToolCallContext,
    ToolContract,
    ToolResult,
    ToolSpec,
)


class ToolRegistry:
    """Own the set of callable tools exposed to the model."""

    def __init__(self, contracts: Iterable[ToolContract] = ()) -> None:
        self._contracts: dict[str, ToolContract] = {}
        for contract in contracts:
            self.register(contract)

    def register(self, contract: ToolContract) -> None:
        """Register one tool and reject duplicate names deterministically."""

        name = contract.spec.name
        if name in self._contracts:
            raise ValueError(f"Tool '{name}' is already registered.")
        self._contracts[name] = contract

    def unregister(self, name: str) -> ToolContract:
        """Remove and return a registered tool."""

        try:
            return self._contracts.pop(name)
        except KeyError as error:
            raise KeyError(f"Tool '{name}' is not registered.") from error

    def get(self, name: str) -> ToolContract | None:
        return self._contracts.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._contracts

    def __len__(self) -> int:
        return len(self._contracts)

    def specs(self) -> tuple[ToolSpec, ...]:
        """Return stable registration-order metadata for model binding."""

        return tuple(contract.spec for contract in self._contracts.values())

    def contracts(self) -> Mapping[str, ToolContract]:
        """Return a snapshot of the current registered contracts."""

        return self._contracts.copy()


class ToolDispatcher:
    """Validate, execute, and normalize one model-requested tool call."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        authorization: ToolAuthorization | None = None,
        hooks: RuntimeHook | HookSet | Iterable[RuntimeHook] | None = None,
    ) -> None:
        self.registry = registry
        self.authorization = authorization or PermissionPolicy()
        self.hooks = hooks if isinstance(hooks, HookSet) else HookSet(hooks)

    def dispatch(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolCallContext,
        *,
        timeout_seconds: float | None = None,
        tool_call_id: str | None = None,
    ) -> ToolResult:
        """Dispatch a tool without allowing unknown names to escape as errors."""

        started_at = monotonic()
        call_id = tool_call_id or f"dispatch:{name}"
        contract = self.registry.get(name)
        self.hooks.emit(
            RuntimeEvent(
                kind="tool",
                phase="before",
                request_id=context.request_id,
                user_id=context.user_id,
                session_key=context.session_key,
                tool_name=name,
                tool_call_id=call_id,
                permission_level=(
                    str(contract.spec.permission_level) if contract is not None else None
                ),
                tool_input_digest=digest_tool_input(arguments),
                metadata={"argument_count": len(arguments)},
            )
        )

        def finish(result: ToolResult) -> ToolResult:
            self.hooks.emit(
                RuntimeEvent(
                    kind="tool",
                    phase="after",
                    request_id=context.request_id,
                    user_id=context.user_id,
                    session_key=context.session_key,
                    tool_name=name,
                    tool_call_id=call_id,
                    permission_level=(
                        str(contract.spec.permission_level) if contract is not None else None
                    ),
                    tool_input_digest=digest_tool_input(arguments),
                    duration_seconds=monotonic() - started_at,
                    ok=result.ok,
                    error_code=result.error.code if result.error else None,
                )
            )
            # M4 audit persistence is best-effort and never changes the tool result.
            try:
                from database.db import record_tool_audit

                error_code = result.error.code if result.error else None
                if result.ok:
                    audit_status = "success"
                elif error_code == ErrorCode.TOOL_TIMEOUT:
                    audit_status = "timeout"
                elif error_code in {
                    ErrorCode.PERMISSION_DENIED,
                    ErrorCode.CONFIRMATION_REQUIRED,
                    ErrorCode.CONFIRMATION_DENIED,
                    ErrorCode.CONFIRMATION_FAILED,
                }:
                    audit_status = "denied"
                else:
                    audit_status = "failed"
                record_tool_audit(
                    context.user_id,
                    request_id=context.request_id,
                    session_key=context.session_key,
                    tool_call_id=call_id,
                    tool_name=name,
                    permission_level=(
                        contract.spec.permission_level.value
                        if contract is not None
                        else "read"
                    ),
                    input_digest=digest_tool_input(arguments),
                    args_summary={
                        "argument_count": len(arguments),
                        "tool_call_id": call_id,
                    },
                    success=result.ok,
                    status=audit_status,
                    duration_ms=max(0, int((monotonic() - started_at) * 1000)),
                    error_code=error_code,
                )
            except Exception:
                # Runtime observability must not turn a business result into an error.
                pass
            return result

        if contract is None:
            return finish(ToolResult.failure(
                ErrorCode.UNKNOWN_TOOL,
                f"Tool '{name}' is not registered.",
                request_id=context.request_id,
            ))

        if self.authorization is not None:
            call = ToolCall(id=call_id, name=name, arguments=dict(arguments))
            authorization_result = self.authorization.authorize(
                call,
                contract.spec,
                context,
            )
            permission_value = getattr(
                contract.spec.permission_level,
                "value",
                contract.spec.permission_level,
            )
            if permission_value == PermissionLevel.HIGH_RISK.value:
                self.hooks.emit(
                    RuntimeEvent(
                        kind="high_risk_confirm",
                        phase="occurred",
                        request_id=context.request_id,
                        user_id=context.user_id,
                        session_key=context.session_key,
                        tool_name=name,
                        tool_call_id=call_id,
                        permission_level=str(contract.spec.permission_level),
                        tool_input_digest=digest_tool_input(arguments),
                        ok=authorization_result is None,
                        error_code=(
                            authorization_result.error.code
                            if authorization_result is not None
                            else None
                        ),
                        metadata={
                            "decision": (
                                "allowed"
                                if authorization_result is None
                                else "denied"
                            ),
                            "reason": (
                                str(authorization_result.error.details.get("reason", ""))
                                if authorization_result is not None
                                else "confirmed"
                            ),
                        },
                    )
                )
            if authorization_result is not None:
                return finish(authorization_result)

        execution_timeout = contract.spec.timeout_seconds
        if timeout_seconds is not None:
            execution_timeout = min(execution_timeout, timeout_seconds)

        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="todo-tool")
        future = executor.submit(contract.execute, arguments, context)
        deadline = monotonic() + execution_timeout
        try:
            while True:
                token = context.cancellation_token
                if token is not None and token.cancelled:
                    future.cancel()
                    return finish(ToolResult.failure(
                        ErrorCode.CANCELLED,
                        "Tool execution was cancelled.",
                        request_id=context.request_id,
                    ))
                remaining = deadline - monotonic()
                if remaining <= 0:
                    future.cancel()
                    return finish(ToolResult.failure(
                        ErrorCode.TOOL_TIMEOUT,
                        f"Tool '{name}' exceeded its execution timeout.",
                        retryable=True,
                        request_id=context.request_id,
                    ))
                try:
                    result = future.result(timeout=min(remaining, 0.05))
                    if token is not None and token.cancelled:
                        return finish(ToolResult.failure(
                            ErrorCode.CANCELLED,
                            "Tool execution was cancelled.",
                            request_id=context.request_id,
                        ))
                    return finish(result)
                except FutureTimeout:
                    continue
                except Exception:
                    return finish(ToolResult.failure(
                        ErrorCode.TOOL_DISPATCH_FAILED,
                        "Tool dispatch failed unexpectedly.",
                        request_id=context.request_id,
                    ))
        finally:
            # Python cannot forcibly stop a handler already running in a thread.
            # The caller gets a timeout result; the handler may still finish.
            executor.shutdown(wait=False, cancel_futures=True)
