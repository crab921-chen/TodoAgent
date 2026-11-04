"""Budgeted model-to-tool Agent Loop for the Harness runtime."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from time import monotonic
from typing import Mapping, Protocol, Sequence

from pydantic import BaseModel, ConfigDict, Field

from harness.context import ContextAssembler
from harness.hooks import HookSet, RuntimeEvent, RuntimeHook
from harness.messages import LoopMessage, ModelResponse, ToolCall
from harness.security import PermissionPolicy, ToolAuthorization
from harness.tool_protocol import (
    CancellationToken,
    ErrorCode,
    ToolCallContext,
    ToolContract,
    ToolError,
    ToolResult,
    ToolSpec,
)
from harness.tool_registry import ToolDispatcher, ToolRegistry


class ModelInvoker(Protocol):
    """The narrow model boundary required by AgentLoop."""

    def complete(
        self,
        messages: Sequence[LoopMessage],
        tools: Sequence[ToolSpec],
        cancellation_token: CancellationToken,
    ) -> ModelResponse:
        ...


class LoopBudget(BaseModel):
    """Hard limits for one user request."""

    model_config = ConfigDict(extra="forbid")

    max_tool_calls: int = Field(default=8, ge=1, le=100)
    max_model_calls: int = Field(default=16, ge=1, le=100)
    max_model_tokens: int | None = Field(default=None, ge=1)
    max_total_seconds: float = Field(default=120, gt=0, le=3600)
    model_timeout_seconds: float = Field(default=60, gt=0, le=600)


class CancellationRequested(Exception):
    """Internal signal used to unwind a cooperative cancellation wait."""


@dataclass(frozen=True)
class AgentLoopResult:
    """Final loop state, including messages and structured tool results."""

    final_response: str | None
    messages: tuple[LoopMessage, ...]
    tool_results: tuple[ToolResult, ...]
    tool_call_count: int
    error: ToolError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class AgentLoop:
    """Run model responses and tool calls until a final model response."""

    def __init__(
        self,
        model: ModelInvoker,
        tools: Mapping[str, ToolContract] | ToolRegistry,
        *,
        system_instruction: str = "You are a helpful assistant.",
        context_assembler: ContextAssembler | None = None,
        budget: LoopBudget | None = None,
        max_tool_calls: int | None = None,
        authorization: ToolAuthorization | None = None,
        hooks: RuntimeHook | HookSet | Sequence[RuntimeHook] | None = None,
    ) -> None:
        self.model = model
        self.registry = tools if isinstance(tools, ToolRegistry) else ToolRegistry(tools.values())
        self.hooks = hooks if isinstance(hooks, HookSet) else HookSet(hooks)
        self.authorization = authorization or PermissionPolicy()
        self.dispatcher = ToolDispatcher(
            self.registry,
            authorization=self.authorization,
            hooks=self.hooks,
        )
        self.context_assembler = context_assembler or ContextAssembler(system_instruction)
        self.budget = budget or LoopBudget()
        if max_tool_calls is not None:
            self.budget = self.budget.model_copy(update={"max_tool_calls": max_tool_calls})

    @property
    def tool_specs(self) -> tuple[ToolSpec, ...]:
        """Expose only tool metadata to the model boundary."""

        return self.registry.specs()

    def run(
        self,
        user_input: str,
        context: ToolCallContext,
        *,
        history: Sequence[LoopMessage] = (),
        cancellation_token: CancellationToken | None = None,
    ) -> AgentLoopResult:
        """Process one user request through model and tool turns."""

        token = cancellation_token or context.cancellation_token or CancellationToken()
        context = replace(context, cancellation_token=token)
        messages = list(
            self.context_assembler.assemble(
                current_input=user_input,
                history=history,
            )
        )
        tool_results: list[ToolResult] = []
        tool_call_count = 0
        model_call_count = 0
        model_token_count = 0
        started_at = monotonic()

        while True:
            if token.cancelled:
                self._emit_lifecycle(context, "cancellation", ErrorCode.CANCELLED)
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.CANCELLED, "Agent loop was cancelled.")
            remaining = self.budget.max_total_seconds - (monotonic() - started_at)
            if remaining <= 0:
                self._emit_lifecycle(context, "budget_exceeded", ErrorCode.TOTAL_TIME_LIMIT_REACHED)
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.TOTAL_TIME_LIMIT_REACHED, "Agent loop time limit reached.")
            if model_call_count >= self.budget.max_model_calls:
                self._emit_lifecycle(context, "budget_exceeded", ErrorCode.MODEL_CALL_LIMIT_REACHED)
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.MODEL_CALL_LIMIT_REACHED, "Model call limit reached.")

            model_call_count += 1
            model_started_at = monotonic()
            self.hooks.emit(
                RuntimeEvent(
                    kind="model",
                    phase="before",
                    request_id=context.request_id,
                    user_id=context.user_id,
                    session_key=context.session_key,
                    model_name=self._model_name(),
                    metadata={"message_count": len(messages), "tool_count": len(self.tool_specs)},
                )
            )
            try:
                response = self._invoke_model(
                    tuple(messages),
                    min(remaining, self.budget.model_timeout_seconds),
                    token,
                )
                if response.total_tokens is not None:
                    model_token_count += response.total_tokens
                    if (
                        self.budget.max_model_tokens is not None
                        and model_token_count > self.budget.max_model_tokens
                    ):
                        self._emit_model_after(
                            context,
                            model_started_at,
                            error_code=ErrorCode.MODEL_TOKEN_BUDGET_EXCEEDED,
                            token_usage=response.total_tokens,
                            metadata={"total_tokens": response.total_tokens},
                        )
                        self._emit_lifecycle(context, "budget_exceeded", ErrorCode.MODEL_TOKEN_BUDGET_EXCEEDED)
                        return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.MODEL_TOKEN_BUDGET_EXCEEDED, "Model token budget exceeded.")
            except FutureTimeout:
                error_code = (
                    ErrorCode.TOTAL_TIME_LIMIT_REACHED
                    if monotonic() - started_at >= self.budget.max_total_seconds
                    else ErrorCode.MODEL_TIMEOUT
                )
                self._emit_model_after(context, model_started_at, error_code=error_code)
                if monotonic() - started_at >= self.budget.max_total_seconds:
                    self._emit_lifecycle(context, "budget_exceeded", ErrorCode.TOTAL_TIME_LIMIT_REACHED)
                    return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.TOTAL_TIME_LIMIT_REACHED, "Agent loop time limit reached.")
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.MODEL_TIMEOUT, "Model execution exceeded its timeout.")
            except CancellationRequested:
                self._emit_model_after(context, model_started_at, error_code=ErrorCode.CANCELLED)
                self._emit_lifecycle(context, "cancellation", ErrorCode.CANCELLED)
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.CANCELLED, "Agent loop was cancelled.")
            except Exception:
                self._emit_model_after(context, model_started_at, error_code=ErrorCode.MODEL_EXECUTION_FAILED)
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.MODEL_EXECUTION_FAILED, "Model execution failed.")

            self._emit_model_after(
                context,
                model_started_at,
                ok=True,
                token_usage=response.total_tokens,
                metadata={
                    "tool_call_count": len(response.tool_calls),
                    "total_tokens": response.total_tokens,
                },
            )

            if token.cancelled:
                self._emit_lifecycle(context, "cancellation", ErrorCode.CANCELLED)
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.CANCELLED, "Agent loop was cancelled.")

            if monotonic() - started_at >= self.budget.max_total_seconds:
                self._emit_lifecycle(context, "budget_exceeded", ErrorCode.TOTAL_TIME_LIMIT_REACHED)
                return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.TOTAL_TIME_LIMIT_REACHED, "Agent loop time limit reached.")

            messages.append(
                LoopMessage(
                    role="assistant",
                    content=response.content,
                    tool_calls=response.tool_calls,
                )
            )

            if not response.tool_calls:
                return AgentLoopResult(
                    final_response=response.content,
                    messages=tuple(messages),
                    tool_results=tuple(tool_results),
                    tool_call_count=tool_call_count,
                )

            if len(response.tool_calls) > self.budget.max_tool_calls - tool_call_count:
                self._emit_lifecycle(context, "budget_exceeded", ErrorCode.TOOL_CALL_LIMIT_REACHED)
                return self._failure_result(
                    messages,
                    tool_results,
                    tool_call_count,
                    ErrorCode.TOOL_CALL_LIMIT_REACHED,
                    "Tool call limit would be exceeded by this model response.",
                )

            for call in response.tool_calls:
                remaining = self.budget.max_total_seconds - (monotonic() - started_at)
                if token.cancelled:
                    self._emit_lifecycle(context, "cancellation", ErrorCode.CANCELLED)
                    return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.CANCELLED, "Agent loop was cancelled.")
                if remaining <= 0:
                    self._emit_lifecycle(context, "budget_exceeded", ErrorCode.TOTAL_TIME_LIMIT_REACHED)
                    return self._failure_result(messages, tool_results, tool_call_count, ErrorCode.TOTAL_TIME_LIMIT_REACHED, "Agent loop time limit reached.")
                result = self.dispatcher.dispatch(
                    call.name,
                    call.arguments,
                    context,
                    timeout_seconds=remaining,
                    tool_call_id=call.id,
                )

                tool_call_count += 1
                tool_results.append(result)
                messages.append(
                    LoopMessage(
                        role="tool",
                        content=result.model_dump(mode="json"),
                        tool_call_id=call.id,
                        name=call.name,
                    )
                )
                messages = list(self.context_assembler.bound(messages))

    def _emit_model_after(
        self,
        context: ToolCallContext,
        started_at: float,
        *,
        ok: bool | None = None,
        error_code: str | ErrorCode | None = None,
        token_usage: int | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.hooks.emit(
            RuntimeEvent(
                kind="model",
                phase="after",
                request_id=context.request_id,
                user_id=context.user_id,
                session_key=context.session_key,
                duration_seconds=monotonic() - started_at,
                model_name=self._model_name(),
                token_usage=token_usage,
                ok=ok,
                error_code=str(error_code) if error_code is not None else None,
                metadata=metadata or {},
            )
        )

    def _emit_lifecycle(
        self,
        context: ToolCallContext,
        kind: str,
        error_code: str | ErrorCode,
        *,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.hooks.emit(
            RuntimeEvent(
                kind=kind,  # type: ignore[arg-type]
                phase="occurred",
                request_id=context.request_id,
                user_id=context.user_id,
                session_key=context.session_key,
                ok=False,
                error_code=str(error_code),
                metadata=metadata or {},
            )
        )

    def _model_name(self) -> str | None:
        for attribute in ("model_name", "model"):
            value = getattr(self.model, attribute, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def _invoke_model(
        self,
        messages: Sequence[LoopMessage],
        timeout: float,
        cancellation_token: CancellationToken,
    ) -> ModelResponse:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="todo-model")
        future = executor.submit(
            self.model.complete,
            messages,
            self.tool_specs,
            cancellation_token,
        )
        deadline = monotonic() + timeout
        try:
            while True:
                if cancellation_token.cancelled:
                    future.cancel()
                    raise CancellationRequested
                remaining = deadline - monotonic()
                if remaining <= 0:
                    future.cancel()
                    raise FutureTimeout
                try:
                    return future.result(timeout=min(remaining, 0.05))
                except FutureTimeout:
                    continue
        finally:
            # This limits how long the loop waits; it cannot kill a running call.
            executor.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _failure_result(
        messages: Sequence[LoopMessage],
        tool_results: Sequence[ToolResult],
        tool_call_count: int,
        code: str | ErrorCode,
        message: str,
    ) -> AgentLoopResult:
        return AgentLoopResult(
            final_response=None,
            messages=tuple(messages),
            tool_results=tuple(tool_results),
            tool_call_count=tool_call_count,
            error=ToolError(code=code, message=message),
        )
