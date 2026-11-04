"""M6-M7 conversation persistence and workbench tool tests."""

from __future__ import annotations

import unittest
from pathlib import Path
from uuid import uuid4

import database.db as db
from database.conversations import (
    ConversationNotFoundError,
    append_message,
    archive_conversation,
    create_conversation,
    list_conversations,
    list_messages,
    record_tool_turn,
)
from database.migrations import apply_migrations, open_connection
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry


class ConversationPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_path = Path.cwd() / f".m6-test-{uuid4().hex}.db"
        apply_migrations(self.database_path)
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = self.database_path

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.original_database_path
        self.database_path.unlink(missing_ok=True)

    def test_m6_migration_creates_conversation_entities(self) -> None:
        connection = open_connection(self.database_path)
        try:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            connection.close()
        self.assertIn("conversation_messages", tables)
        self.assertIn("conversation_tool_turns", tables)

    def test_m7_messages_are_ordered_idempotent_and_user_scoped(self) -> None:
        conversation = create_conversation("alice", "Main", client_request_id="main")
        first = append_message(
            "alice", conversation["id"], role="user", content="hello", client_message_id="m-1"
        )
        duplicate = append_message(
            "alice", conversation["id"], role="user", content="changed", client_message_id="m-1"
        )
        second = append_message(
            "alice", conversation["id"], role="assistant", content="world"
        )
        self.assertEqual(first["id"], duplicate["id"])
        self.assertEqual([item["sequence_no"] for item in list_messages("alice", conversation["id"])], [1, 2])
        self.assertEqual(second["content"], "world")
        with self.assertRaises(ConversationNotFoundError):
            list_messages("bob", conversation["id"])

    def test_m7_archive_makes_conversation_read_only(self) -> None:
        conversation = create_conversation("alice", "Main")
        self.assertTrue(archive_conversation("alice", conversation["id"]))
        with self.assertRaises(ValueError):
            append_message("alice", conversation["id"], role="user", content="blocked")

    def test_tool_contracts_expose_conversation_operations(self) -> None:
        dispatcher = ToolDispatcher(build_todo_tool_registry())
        context = ToolCallContext(user_id="alice", request_id="m7", session_key="conversation:test")
        result = dispatcher.dispatch("conversation_create", {"title": "Planning"}, context)
        self.assertTrue(result.ok)
        session_id = result.data["data"]["id"]
        appended = dispatcher.dispatch(
            "message_append",
            {"session_id": session_id, "role": "user", "content": "hello"},
            context,
        )
        self.assertTrue(appended.ok)
        messages = dispatcher.dispatch(
            "conversation_messages", {"session_id": session_id}, context
        )
        self.assertTrue(messages.ok)
        self.assertEqual(messages.data["data"][0]["content"], "hello")

    def test_tool_turns_store_summary_without_raw_arguments(self) -> None:
        conversation = create_conversation("alice", "Main")
        message = append_message("alice", conversation["id"], role="tool", content={"ok": True})
        turn_id = record_tool_turn(
            "alice", conversation["id"], request_id="request-1", tool_name="todo_list_tasks",
            status="success", result_summary={"data": [1]}, message_id=message["id"]
        )
        self.assertGreater(turn_id, 0)


if __name__ == "__main__":
    unittest.main()
