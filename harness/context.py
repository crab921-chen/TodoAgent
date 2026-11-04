"""Context assembly and bounded conversation history for the Harness."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Sequence

from harness.messages import LoopMessage


@dataclass(frozen=True)
class ContextLimits:
    """Hard limits applied before messages are sent to the model."""

    max_history_messages: int = 20
    max_message_chars: int = 4000
    max_tool_result_chars: int = 6000
    max_total_chars: int = 20000

    def __post_init__(self) -> None:
        if any(
            value < 1
            for value in (
                self.max_history_messages,
                self.max_message_chars,
                self.max_tool_result_chars,
                self.max_total_chars,
            )
        ):
            raise ValueError("context limits must be positive")


class ContextAssembler:
    """Build a bounded model context with explicit message categories."""

    def __init__(
        self,
        system_instruction: str,
        *,
        limits: ContextLimits | None = None,
    ) -> None:
        if not system_instruction.strip():
            raise ValueError("system_instruction must not be blank")
        self.system_instruction = system_instruction.strip()
        self.limits = limits or ContextLimits()

    def assemble(
        self,
        *,
        current_input: str,
        history: Sequence[LoopMessage] = (),
        runtime_messages: Sequence[LoopMessage] = (),
    ) -> tuple[LoopMessage, ...]:
        """Return system instructions, bounded history, input, and tool turns."""

        system = LoopMessage(
            role="system",
            content=self._clip_text(
                self.system_instruction,
                self.limits.max_message_chars,
            ),
        )
        bounded_history = [
            self._normalize_message(message)
            for message in history
            if message.role != "system"
        ][-self.limits.max_history_messages :]
        current = LoopMessage(
            role="user",
            content=self._clip_text(
                current_input,
                self.limits.max_message_chars,
            ),
        )
        runtime = [self._normalize_message(message) for message in runtime_messages]

        messages = [system, *bounded_history, current, *runtime]
        return self.bound(messages)

    def bound(self, messages: Sequence[LoopMessage]) -> tuple[LoopMessage, ...]:
        """Normalize and trim an already assembled conversation."""

        normalized = [self._normalize_message(message) for message in messages]
        if not normalized:
            return ()
        preserve = {0}
        user_indexes = [
            index for index, message in enumerate(normalized) if message.role == "user"
        ]
        if user_indexes:
            preserve.add(user_indexes[-1])
        return self._fit_total(normalized, preserve=preserve)

    @staticmethod
    def total_size(messages: Sequence[LoopMessage]) -> int:
        """Return the serialized content size used by the total limit."""

        return sum(
            len(json.dumps(message.content, ensure_ascii=False, default=str))
            for message in messages
        )

    def _normalize_message(self, message: LoopMessage) -> LoopMessage:
        limit = self.limits.max_tool_result_chars if message.role == "tool" else self.limits.max_message_chars
        content: Any = message.content
        if isinstance(content, (dict, list, tuple)):
            serialized = json.dumps(content, ensure_ascii=False, default=str)
            if len(serialized) > limit:
                content = {
                    "truncated": True,
                    "preview": serialized[: max(0, limit - 80)],
                }
        elif content is not None:
            content = self._clip_text(str(content), limit)
        return message.model_copy(update={"content": content})

    def _fit_total(
        self,
        messages: list[LoopMessage],
        *,
        preserve: set[int],
    ) -> tuple[LoopMessage, ...]:
        """Drop oldest history first, then compact runtime content."""

        while self._message_size(messages) > self.limits.max_total_chars:
            removable = self._remove_oldest_turn(messages, preserve)
            if removable is None:
                removable = next(
                    (
                        index
                        for index in range(1, len(messages))
                        if index not in preserve
                    ),
                    None,
                )
            if removable is None:
                break
            messages.pop(removable)
            preserve = {index - 1 if index > removable else index for index in preserve if index != removable}

        if self._message_size(messages) > self.limits.max_total_chars:
            remaining = self.limits.max_total_chars
            for index, message in enumerate(messages):
                if index in preserve:
                    continue
                content = str(message.content)
                allowance = max(0, remaining - 64)
                clipped = self._clip_text(content, allowance) if allowance else ""
                messages[index] = message.model_copy(update={"content": clipped})
                remaining -= len(json.dumps(clipped, ensure_ascii=False, default=str))

        if self._message_size(messages) > self.limits.max_total_chars:
            system_index = 0
            user_index = max(
                (i for i, message in enumerate(messages) if message.role == "user"),
                default=None,
            )
            fixed_size = len(json.dumps(messages[system_index].content, ensure_ascii=False, default=str))
            if user_index is not None and user_index != system_index:
                fixed_size += len(json.dumps(messages[user_index].content, ensure_ascii=False, default=str))
            available = max(0, self.limits.max_total_chars - fixed_size)
            if user_index is not None and user_index != system_index:
                user_content = str(messages[user_index].content)
                messages[user_index] = messages[user_index].model_copy(
                    update={"content": self._clip_text(user_content, available)}
                )

        return tuple(messages)

    @staticmethod
    def _remove_oldest_turn(messages: list[LoopMessage], preserve: set[int]) -> int | None:
        for index, message in enumerate(messages):
            if index in preserve or message.role != "assistant" or not message.tool_calls:
                continue
            call_ids = {call.id for call in message.tool_calls}
            for result_index in range(len(messages) - 1, index, -1):
                result = messages[result_index]
                if result.role == "tool" and result.tool_call_id in call_ids:
                    messages.pop(result_index)
                    updated = {
                        item - 1 if item > result_index else item
                        for item in preserve
                    }
                    preserve.clear()
                    preserve.update(updated)
            return index
        return next(
            (
                index
                for index, message in enumerate(messages)
                if index not in preserve and message.role != "system"
            ),
            None,
        )

    @staticmethod
    def _message_size(messages: Sequence[LoopMessage]) -> int:
        return ContextAssembler.total_size(messages)

    @staticmethod
    def _clip_text(value: str, limit: int) -> str:
        if len(value) <= limit:
            return value
        return value[: max(0, limit - 14)] + "...[truncated]"
