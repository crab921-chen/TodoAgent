"""Harness tool contracts for durable conversation management."""

from __future__ import annotations

from typing import Any

from database.conversations import (
    ConversationNotFoundError,
    archive_conversation,
    create_conversation,
    list_conversations,
    list_messages,
    rename_conversation,
)
from harness.tool_models import (
    ConversationArchiveInput,
    ConversationCreateInput,
    ConversationListInput,
    ConversationMessagesInput,
    ConversationRenameInput,
    MessageAppendInput,
    ToolData,
)
from harness.tool_protocol import PermissionLevel, ToolCallContext, ToolContract, ToolExecutionError, ToolSpec


def _data(value: Any) -> dict[str, Any]:
    return {"data": value}


def _error(error: Exception) -> ToolExecutionError:
    if isinstance(error, ConversationNotFoundError):
        return ToolExecutionError("CONVERSATION_NOT_FOUND", str(error))
    return ToolExecutionError("INVALID_ARGUMENTS", str(error))


def _create(arguments: ConversationCreateInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(create_conversation(context.user_id, arguments.title, client_request_id=arguments.client_request_id))
    except (ConversationNotFoundError, ValueError) as error:
        raise _error(error) from error


def _list(arguments: ConversationListInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(list_conversations(context.user_id, status=arguments.status, limit=arguments.limit, offset=arguments.offset))
    except ValueError as error:
        raise _error(error) from error


def _messages(arguments: ConversationMessagesInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(list_messages(context.user_id, arguments.session_id, limit=arguments.limit, offset=arguments.offset))
    except (ConversationNotFoundError, ValueError) as error:
        raise _error(error) from error


def _rename(arguments: ConversationRenameInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(rename_conversation(context.user_id, arguments.session_id, arguments.title))
    except (ConversationNotFoundError, ValueError) as error:
        raise _error(error) from error


def _archive(arguments: ConversationArchiveInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        if not archive_conversation(context.user_id, arguments.session_id):
            raise ConversationNotFoundError("conversation was not found")
        return _data({"session_id": arguments.session_id, "archived": True})
    except ConversationNotFoundError as error:
        raise _error(error) from error


def _append(arguments: MessageAppendInput, context: ToolCallContext) -> dict[str, Any]:
    from database.conversations import append_message

    try:
        return _data(append_message(
            context.user_id,
            arguments.session_id,
            role=arguments.role,
            content=arguments.content,
            message_type=arguments.message_type,
            client_message_id=arguments.client_message_id,
            tool_call_id=arguments.tool_call_id,
            metadata=arguments.metadata,
        ))
    except (ConversationNotFoundError, ValueError) as error:
        raise _error(error) from error


def conversation_tool_contracts() -> tuple[ToolContract, ...]:
    return (
        ToolContract(ToolSpec(name="conversation_create", description="Create a user-owned conversation.", input_model=ConversationCreateInput, output_model=ToolData, permission_level=PermissionLevel.WRITE), _create),
        ToolContract(ToolSpec(name="conversation_list", description="List conversations owned by the user.", input_model=ConversationListInput, output_model=ToolData), _list),
        ToolContract(ToolSpec(name="conversation_messages", description="List messages from an owned conversation.", input_model=ConversationMessagesInput, output_model=ToolData), _messages),
        ToolContract(ToolSpec(name="conversation_rename", description="Rename an owned conversation.", input_model=ConversationRenameInput, output_model=ToolData, permission_level=PermissionLevel.WRITE), _rename),
        ToolContract(ToolSpec(name="conversation_archive", description="Archive an owned conversation.", input_model=ConversationArchiveInput, output_model=ToolData, permission_level=PermissionLevel.HIGH_RISK), _archive),
        ToolContract(ToolSpec(name="message_append", description="Append a message to an owned conversation.", input_model=MessageAppendInput, output_model=ToolData, permission_level=PermissionLevel.WRITE), _append),
    )


__all__ = ["conversation_tool_contracts"]
