"""User-scoped conversation persistence for the M6-M7 workbench."""

from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from database import db


class ConversationNotFoundError(LookupError):
    """The conversation is absent or is not owned by the current user."""


SESSION_STATUSES = frozenset({"active", "archived"})
MESSAGE_ROLES = frozenset({"user", "assistant", "tool", "system"})
TOOL_TURN_STATUSES = frozenset({"started", "success", "failed", "denied", "timeout"})


def _text(value: str, field: str, maximum: int) -> str:
    result = str(value).strip()
    if not result:
        raise ValueError(f"{field} must not be blank")
    if len(result) > maximum:
        raise ValueError(f"{field} is too long")
    return result


def _user_pk(connection, user_id: str, *, create: bool) -> int | None:
    value = db._validate_user_id(user_id)
    if create:
        return db._ensure_user(connection, value)
    row = connection.execute(
        "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
        (value,),
    ).fetchone()
    return None if row is None else int(row["id"])


def _owned_session(connection, user_pk: int, session_id: int):
    return connection.execute(
        "SELECT * FROM agent_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_pk),
    ).fetchone()


def _serialize_session(row, user_id: str) -> dict[str, Any]:
    data = dict(row)
    data["user_id"] = user_id
    data["title"] = data.get("title") or data["session_key"]
    return data


def create_conversation(
    user_id: str,
    title: str,
    *,
    client_request_id: str | None = None,
) -> dict[str, Any]:
    title = _text(title, "title", 200)
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        session_key = _text(client_request_id or f"conversation:{uuid4().hex}", "session_key", 200)
        connection.execute(
            """
            INSERT INTO agent_sessions(user_id, session_key, title, status)
            VALUES (?, ?, ?, 'active')
            ON CONFLICT(user_id, session_key) DO UPDATE SET
                title = excluded.title,
                status = 'active',
                archived_at = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            """,
            (user_pk, session_key, title),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM agent_sessions WHERE user_id = ? AND session_key = ?",
            (user_pk, session_key),
        ).fetchone()
        assert row is not None
        return _serialize_session(row, user_id)
    finally:
        connection.close()


def list_conversations(
    user_id: str,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be 1..500 and offset must be non-negative")
    if status is not None and status not in SESSION_STATUSES:
        raise ValueError("unsupported conversation status")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return []
        sql = "SELECT * FROM agent_sessions WHERE user_id = ?"
        params: list[Any] = [user_pk]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        return [_serialize_session(row, user_id) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def rename_conversation(user_id: str, session_id: int, title: str) -> dict[str, Any]:
    title = _text(title, "title", 200)
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None or _owned_session(connection, user_pk, session_id) is None:
            raise ConversationNotFoundError("conversation was not found")
        connection.execute(
            "UPDATE agent_sessions SET title = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND user_id = ?",
            (title, session_id, user_pk),
        )
        connection.commit()
        row = _owned_session(connection, user_pk, session_id)
        assert row is not None
        return _serialize_session(row, user_id)
    finally:
        connection.close()


def archive_conversation(user_id: str, session_id: int) -> bool:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        cursor = connection.execute(
            """
            UPDATE agent_sessions
            SET status = 'archived', archived_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ? AND status = 'active'
            """,
            (session_id, user_pk),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def append_message(
    user_id: str,
    session_id: int,
    *,
    role: str,
    content: Any,
    message_type: str = "message",
    client_message_id: str | None = None,
    tool_call_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if role not in MESSAGE_ROLES:
        raise ValueError("unsupported message role")
    message_type = _text(message_type, "message_type", 50)
    serialized = json.dumps(content, ensure_ascii=False, separators=(",", ":"), default=str)
    digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
    metadata_text = json.dumps(metadata or {}, ensure_ascii=False, separators=(",", ":"), default=str)
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            raise ConversationNotFoundError("conversation was not found")
        session = _owned_session(connection, user_pk, session_id)
        if session is None:
            raise ConversationNotFoundError("conversation was not found")
        if session["status"] == "archived":
            raise ValueError("archived conversation is read-only")
        if client_message_id:
            existing = connection.execute(
                "SELECT * FROM conversation_messages WHERE user_id = ? AND client_message_id = ?",
                (user_pk, client_message_id),
            ).fetchone()
            if existing is not None:
                return _serialize_message(existing)
        sequence = connection.execute(
            "SELECT COALESCE(MAX(sequence_no), 0) + 1 FROM conversation_messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        cursor = connection.execute(
            """
            INSERT INTO conversation_messages(
                session_id, user_id, sequence_no, role, message_type, content,
                content_digest, client_message_id, tool_call_id, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, user_pk, sequence, role, message_type, serialized, digest,
             client_message_id, tool_call_id, metadata_text),
        )
        connection.execute(
            "UPDATE agent_sessions SET updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
            (session_id,),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM conversation_messages WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        assert row is not None
        return _serialize_message(row)
    finally:
        connection.close()


def _serialize_message(row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["content"] = json.loads(data["content"]) if data["content"] is not None else None
    except (TypeError, json.JSONDecodeError):
        pass
    try:
        data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        data["metadata"] = {}
        data.pop("metadata_json", None)
    return data


def list_messages(
    user_id: str,
    session_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be 1..500 and offset must be non-negative")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None or _owned_session(connection, user_pk, session_id) is None:
            raise ConversationNotFoundError("conversation was not found")
        rows = connection.execute(
            "SELECT * FROM conversation_messages WHERE session_id = ? AND user_id = ? ORDER BY sequence_no LIMIT ? OFFSET ?",
            (session_id, user_pk, limit, offset),
        )
        return [_serialize_message(row) for row in rows]
    finally:
        connection.close()


def record_tool_turn(
    user_id: str,
    session_id: int,
    *,
    request_id: str,
    tool_name: str,
    status: str,
    tool_call_id: str | None = None,
    arguments_digest: str | None = None,
    result_summary: Any = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
    message_id: int | None = None,
) -> int:
    if status not in TOOL_TURN_STATUSES:
        raise ValueError("unsupported tool turn status")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None or _owned_session(connection, user_pk, session_id) is None:
            raise ConversationNotFoundError("conversation was not found")
        cursor = connection.execute(
            """
            INSERT INTO conversation_tool_turns(
                message_id, session_id, user_id, request_id, tool_call_id,
                tool_name, arguments_digest, result_summary, status, duration_ms, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (message_id, session_id, user_pk, _text(request_id, "request_id", 200), tool_call_id,
             _text(tool_name, "tool_name", 100), arguments_digest,
             json.dumps(result_summary, ensure_ascii=False, default=str)[:4000]
             if result_summary is not None else None,
             status, duration_ms, error_code),
        )
        connection.commit()
        return int(cursor.lastrowid)
    finally:
        connection.close()


__all__ = [
    "ConversationNotFoundError",
    "append_message",
    "archive_conversation",
    "create_conversation",
    "list_conversations",
    "list_messages",
    "record_tool_turn",
    "rename_conversation",
]
