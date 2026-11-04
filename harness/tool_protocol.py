"""Framework-neutral tool contracts used by the Harness runtime.

This module defines the framework-neutral M1 tool contract. Model calls,
dispatch, permissions, retry execution, and timeouts are handled by the
surrounding Harness components.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Event
from typing import Any, Callable, Mapping, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


InputModel = TypeVar("InputModel", bound=BaseModel)
OutputModel = TypeVar("OutputModel", bound=BaseModel)


class PermissionLevel(StrEnum):
    """Authorization level required before a tool may execute."""

    READ = "read"
    WRITE = "write"
    HIGH_RISK = "high_risk"


class ErrorCode(StrEnum):
    """Stable error codes shared by protocol, dispatcher, and Agent Loop."""

    INVALID_ARGUMENTS = "INVALID_ARGUMENTS"
    INVALID_OUTPUT = "INVALID_OUTPUT"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    TOOL_DISPATCH_FAILED = "TOOL_DISPATCH_FAILED"
    UNKNOWN_TOOL = "UNKNOWN_TOOL"
    TOOL_TIMEOUT = "TOOL_TIMEOUT"
    MODEL_EXECUTION_FAILED = "MODEL_EXECUTION_FAILED"
    MODEL_TIMEOUT = "MODEL_TIMEOUT"
    MODEL_CALL_LIMIT_REACHED = "MODEL_CALL_LIMIT_REACHED"
    MODEL_TOKEN_BUDGET_EXCEEDED = "MODEL_TOKEN_BUDGET_EXCEEDED"
    TOOL_CALL_LIMIT_REACHED = "TOOL_CALL_LIMIT_REACHED"
    TOTAL_TIME_LIMIT_REACHED = "TOTAL_TIME_LIMIT_REACHED"
    CANCELLED = "CANCELLED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    CONFIRMATION_REQUIRED = "CONFIRMATION_REQUIRED"
    CONFIRMATION_DENIED = "CONFIRMATION_DENIED"
    CONFIRMATION_FAILED = "CONFIRMATION_FAILED"


class RetryPolicy(BaseModel):
    """Retry metadata consumed by the future tool dispatcher."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0, ge=0, le=300)
    retryable_error_codes: frozenset[str] = Field(default_factory=frozenset)

    @field_validator("retryable_error_codes")
    @classmethod
    def validate_error_codes(cls, value: frozenset[str]) -> frozenset[str]:
        for code in value:
            if not code or code.upper() != code or " " in code:
                raise ValueError("retryable error codes must be non-empty uppercase tokens")
        return value


class ToolSpec(BaseModel):
    """Declarative metadata and schemas for one callable tool."""

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    name: str = Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=2000)
    input_model: type[BaseModel]
    output_model: type[BaseModel] | None = None
    permission_level: PermissionLevel = PermissionLevel.READ
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    @field_validator("description")
    @classmethod
    def validate_description(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def validate_models(self) -> "ToolSpec":
        if not issubclass(self.input_model, BaseModel):
            raise TypeError("input_model must be a Pydantic BaseModel")
        if self.output_model is not None and not issubclass(self.output_model, BaseModel):
            raise TypeError("output_model must be a Pydantic BaseModel")
        return self

    def input_schema(self) -> dict[str, Any]:
        """Return the model-visible JSON Schema for tool arguments."""

        return self.input_model.model_json_schema()

    def output_schema(self) -> dict[str, Any] | None:
        """Return the declared output JSON Schema, if one exists."""

        if self.output_model is None:
            return None
        return self.output_model.model_json_schema()


class ToolError(BaseModel):
    """Stable error envelope returned by a tool contract."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(min_length=1, max_length=80, pattern=r"^[A-Z][A-Z0-9_]*$")
    message: str = Field(min_length=1, max_length=1000)
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """Stable success or failure envelope passed between Harness stages."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    data: Any | None = None
    error: ToolError | None = None
    request_id: str | None = None

    @model_validator(mode="after")
    def validate_envelope(self) -> "ToolResult":
        if self.ok and self.error is not None:
            raise ValueError("successful result cannot contain an error")
        if not self.ok and self.error is None:
            raise ValueError("failed result must contain an error")
        return self

    @classmethod
    def success(cls, data: Any = None, request_id: str | None = None) -> "ToolResult":
        return cls(ok=True, data=data, request_id=request_id)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> "ToolResult":
        return cls(
            ok=False,
            error=ToolError(
                code=code,
                message=message,
                retryable=retryable,
                details=details or {},
            ),
            request_id=request_id,
        )


class ToolExecutionError(Exception):
    """Expected business/tool error that can be exposed safely to the caller."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details or {}


@dataclass(frozen=True)
class ToolCallContext:
    """Trusted request context supplied by the runtime, not by the model."""

    user_id: str
    request_id: str
    session_key: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    cancellation_token: "CancellationToken | None" = None


class CancellationToken:
    """Thread-safe cooperative cancellation signal for one request."""

    def __init__(self) -> None:
        self._event = Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for cancellation; return whether cancellation was requested."""

        return self._event.wait(timeout)


ToolHandler = Callable[[BaseModel, ToolCallContext], Any]


class ToolContract:
    """Bind a validated tool specification to one independent handler."""

    def __init__(self, spec: ToolSpec, handler: ToolHandler) -> None:
        self.spec = spec
        self.handler = handler

    def validate_input(self, raw_input: Mapping[str, Any]) -> BaseModel:
        """Validate and normalize untrusted model arguments."""

        return self.spec.input_model.model_validate(raw_input)

    def execute(
        self,
        raw_input: Mapping[str, Any],
        context: ToolCallContext,
    ) -> ToolResult:
        """Validate input, invoke the handler once, and normalize its result.

        Retry metadata and runtime authorization/timeout enforcement belong to
        the dispatcher; this contract remains a single-call boundary.
        """

        try:
            validated_input = self.validate_input(raw_input)
        except ValidationError as error:
            return ToolResult.failure(
                ErrorCode.INVALID_ARGUMENTS,
                "Tool arguments failed schema validation.",
                details={"validation_errors": error.errors()},
                request_id=context.request_id,
            )

        try:
            output = self.handler(validated_input, context)
            if self.spec.output_model is not None:
                output = self.spec.output_model.model_validate(output)
                output = output.model_dump(mode="json")
            return ToolResult.success(output, request_id=context.request_id)
        except ToolExecutionError as error:
            return ToolResult.failure(
                error.code,
                error.message,
                retryable=error.retryable,
                details=error.details,
                request_id=context.request_id,
            )
        except ValidationError as error:
            return ToolResult.failure(
                ErrorCode.INVALID_OUTPUT,
                "Tool returned data that failed its output schema.",
                details={"validation_errors": error.errors()},
                request_id=context.request_id,
            )
        except Exception:
            return ToolResult.failure(
                ErrorCode.TOOL_EXECUTION_FAILED,
                "Tool execution failed.",
                retryable=False,
                request_id=context.request_id,
            )
