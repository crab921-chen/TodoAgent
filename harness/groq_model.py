"""Groq adapter for the framework-neutral Harness AgentLoop."""

from __future__ import annotations

import json
from typing import Any, Sequence
from uuid import uuid4

from harness.messages import LoopMessage, ModelResponse, ToolCall
from harness.tool_protocol import CancellationToken, ToolSpec


class GroqModelInvoker:
    """Translate Harness messages and tool specs to Groq chat completions."""

    def __init__(
        self,
        api_key: str,
        *,
        model_name: str,
        temperature: float = 0.0,
        client: Any | None = None,
    ) -> None:
        if not str(api_key).strip():
            raise ValueError("api_key must not be blank")
        if not str(model_name).strip():
            raise ValueError("model_name must not be blank")
        self.model_name = str(model_name).strip()
        self.temperature = float(temperature)
        if client is None:
            from groq import Groq

            client = Groq(api_key=api_key)
        self.client = client

    def complete(
        self,
        messages: Sequence[LoopMessage],
        tools: Sequence[ToolSpec],
        cancellation_token: CancellationToken,
    ) -> ModelResponse:
        if cancellation_token.cancelled:
            raise RuntimeError("model call cancelled")
        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=[self._message(message) for message in messages],
            tools=[self._tool(spec) for spec in tools],
            temperature=self.temperature,
        )
        if cancellation_token.cancelled:
            raise RuntimeError("model call cancelled")
        message = response.choices[0].message
        calls = [
            ToolCall(
                id=str(getattr(call, "id", "") or uuid4().hex),
                name=str(call.function.name),
                arguments=self._arguments(call.function.arguments),
            )
            for call in (getattr(message, "tool_calls", None) or ())
        ]
        usage = getattr(response, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        return ModelResponse(
            content=getattr(message, "content", None),
            tool_calls=calls,
            total_tokens=total_tokens,
        )

    @staticmethod
    def _message(message: LoopMessage) -> dict[str, Any]:
        content = message.content
        if isinstance(content, (dict, list, tuple)):
            content = json.dumps(content, ensure_ascii=False, default=str)
        payload: dict[str, Any] = {
            "role": message.role,
            "content": "" if content is None else str(content),
        }
        if message.role == "assistant" and message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(
                            call.arguments,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                    },
                }
                for call in message.tool_calls
            ]
        if message.role == "tool":
            payload["tool_call_id"] = message.tool_call_id or "unknown"
            if message.name:
                payload["name"] = message.name
        return payload

    @staticmethod
    def _tool(spec: ToolSpec) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.input_schema(),
            },
        }

    @staticmethod
    def _arguments(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        parsed = json.loads(str(value or "{}"))
        if not isinstance(parsed, dict):
            raise ValueError("model tool arguments must be a JSON object")
        return parsed


__all__ = ["GroqModelInvoker"]
