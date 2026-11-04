"""UI-facing assembly of the stable Harness runtime and conversations."""

from __future__ import annotations

import re
from uuid import uuid4

import streamlit as st

from database.conversations import (
    append_message,
    create_conversation,
    list_conversations,
    list_messages,
    record_tool_turn,
)
from harness.agent_loop import AgentLoop, LoopBudget
from harness.hooks import RecordingHook
from harness.instructions import SYSTEM_INSTRUCTION
from harness.messages import LoopMessage, ToolCall
from harness.openai_compatible_model import OpenAICompatibleModelInvoker
from harness.security import PermissionPolicy
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry
from ops.runtime import RuntimeConfig


def normalize_user_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return normalized[:200]


def request_id(prefix: str = "ui") -> str:
    return f"{prefix}:{uuid4().hex}"


def active_context(user_id: str, *, prefix: str = "ui") -> ToolCallContext:
    return ToolCallContext(user_id=user_id, request_id=request_id(prefix), session_key=f"streamlit:{user_id}")


def active_context_for_conversation(user_id: str, session_id: int, *, prefix: str = "ui") -> ToolCallContext:
    return ToolCallContext(user_id=user_id, request_id=request_id(prefix), session_key=f"conversation:{session_id}")


@st.cache_resource
def _tool_registry():
    """Cache static tool contracts while keeping dispatch authorization per call."""

    return build_todo_tool_registry()


def dispatcher_for(user_id: str) -> ToolDispatcher:
    confirmed = bool(st.session_state.get("confirm_risky_actions", False))
    return ToolDispatcher(
        _tool_registry(),
        authorization=PermissionPolicy(confirmation_callback=lambda context, call, spec: confirmed),
    )


def dispatch(user_id: str, name: str, arguments: dict, *, prefix: str = "ui"):
    return dispatcher_for(user_id).dispatch(name, arguments, active_context(user_id, prefix=prefix))


def data_or_error(result):
    if result.ok:
        return result.data["data"]
    st.error(f"{result.error.code}: {result.error.message}")
    return None


@st.cache_resource
def load_model(api_key: str, model_name: str, base_url: str, temperature: float):
    return OpenAICompatibleModelInvoker(api_key, model_name=model_name, base_url=base_url, temperature=temperature)


def ensure_conversation(user_id: str) -> dict:
    session_id = st.session_state.get("conversation_id")
    conversations = list_conversations(user_id, status="active", limit=500)
    for item in conversations:
        if session_id and int(item["id"]) == int(session_id):
            return item
    selected = conversations[0] if conversations else create_conversation(
        user_id, "主会话", client_request_id=f"streamlit:{user_id}:main"
    )
    st.session_state["conversation_id"] = int(selected["id"])
    return selected


def conversation_options(user_id: str) -> list[dict]:
    return list_conversations(user_id, status="active", limit=500)


def load_conversation_history(user_id: str, session_id: int) -> list[LoopMessage]:
    history: list[LoopMessage] = []
    for item in list_messages(user_id, session_id, limit=500):
        metadata = item.get("metadata") or {}
        history.append(LoopMessage(
            role=item["role"],
            content=item.get("content"),
            tool_calls=[ToolCall.model_validate(call) for call in metadata.get("tool_calls", [])],
            tool_call_id=item.get("tool_call_id"),
            name=metadata.get("name"),
        ))
    return history


def _persist_new_messages(user_id: str, session_id: int, result, history_length: int) -> None:
    new_messages = list(result.messages[1 + history_length + 1:])
    tool_results = iter(result.tool_results)
    for message in new_messages:
        metadata = {"name": message.name}
        if message.tool_calls:
            metadata["tool_calls"] = [call.model_dump(mode="json") for call in message.tool_calls]
        stored = append_message(
            user_id,
            session_id,
            role=message.role,
            content=message.content,
            message_type="tool_result" if message.role == "tool" else "message",
            tool_call_id=message.tool_call_id,
            metadata=metadata,
        )
        if message.role == "tool":
            result_item = next(tool_results, None)
            if result_item is not None:
                record_tool_turn(
                    user_id,
                    session_id,
                    request_id=result_item.request_id or request_id("tool"),
                    tool_name=message.name or "unknown",
                    status="success" if result_item.ok else "failed",
                    tool_call_id=message.tool_call_id,
                    result_summary=result_item.model_dump(mode="json"),
                    error_code=result_item.error.code if result_item.error else None,
                    message_id=int(stored["id"]),
                )


def run_agent(user_id: str, config: RuntimeConfig, user_input: str):
    if not config.groq_api_key:
        return None, "ZHIPU_API_KEY is not configured. Structured tools remain available."
    conversation = ensure_conversation(user_id)
    session_id = int(conversation["id"])
    history = load_conversation_history(user_id, session_id)
    append_message(user_id, session_id, role="user", content=user_input, client_message_id=request_id("message"))
    loop = AgentLoop(
        load_model(config.groq_api_key, config.model_name, config.model_base_url, config.model_temperature),
        build_todo_tool_registry(),
        system_instruction=SYSTEM_INSTRUCTION,
        budget=LoopBudget(max_tool_calls=config.max_tool_calls, model_timeout_seconds=config.model_timeout_seconds),
        authorization=PermissionPolicy(confirmation_callback=lambda context, call, spec: bool(st.session_state.get("confirm_risky_actions", False))),
        hooks=RecordingHook(),
    )
    result = loop.run(user_input, active_context_for_conversation(user_id, session_id, prefix="agent"), history=history)
    _persist_new_messages(user_id, session_id, result, len(history))
    st.session_state["conversation_history"] = list(result.messages[1:])
    if result.ok:
        return result.final_response or "已完成", None
    return None, f"{result.error.code}: {result.error.message}"


__all__ = [
    "active_context",
    "active_context_for_conversation",
    "conversation_options",
    "data_or_error",
    "dispatch",
    "dispatcher_for",
    "ensure_conversation",
    "load_conversation_history",
    "load_model",
    "normalize_user_id",
    "request_id",
    "run_agent",
]
