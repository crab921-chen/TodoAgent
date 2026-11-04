"""OpenAI-compatible chat-completions adapter for Harness models."""

from __future__ import annotations

import json
from typing import Any, Sequence
from uuid import uuid4

import httpx

from harness.messages import LoopMessage, ModelResponse, ToolCall
from harness.tool_protocol import CancellationToken, ToolSpec


class OpenAICompatibleModelInvoker:
    """Call providers exposing the OpenAI-compatible chat API."""

    def __init__(
        self,
        api_key: str,
        *,
        model_name: str,
        base_url: str,
        temperature: float = 0.0,
        timeout: float = 60.0,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key.strip():
            raise ValueError("api_key must not be blank")
        if not model_name.strip():
            raise ValueError("model_name must not be blank")
        if not base_url.strip():
            raise ValueError("base_url must not be blank")
        self.model_name = model_name.strip()
        self.base_url = base_url.rstrip("/")
        self.temperature = float(temperature)
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def complete(
        self,
        messages: Sequence[LoopMessage],
        tools: Sequence[ToolSpec],
        cancellation_token: CancellationToken,
    ) -> ModelResponse:
        if cancellation_token.cancelled:
            raise RuntimeError("model call cancelled")
        response = self._client.post(
            "/chat/completions",
            json={
                "model": self.model_name,
                "messages": [self._message(message) for message in messages],
                "tools": [self._tool(spec) for spec in tools],
                "temperature": self.temperature,
            },
        )
        response.raise_for_status()
        data = response.json()
        if cancellation_token.cancelled:
            raise RuntimeError("model call cancelled")
        message = data["choices"][0]["message"]
        calls = [
            ToolCall(
                id=str(call.get("id") or uuid4().hex),
                name=str(call["function"]["name"]),
                arguments=self._arguments(call["function"].get("arguments")),
            )
            for call in message.get("tool_calls", [])
        ]
        usage = data.get("usage") or {}
        return ModelResponse(
            content=message.get("content"),
            tool_calls=calls,
            total_tokens=usage.get("total_tokens"),
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
                            call.arguments, ensure_ascii=False, separators=(",", ":")
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


__all__ = ["OpenAICompatibleModelInvoker"]
