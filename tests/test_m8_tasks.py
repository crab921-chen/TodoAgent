"""M8 unified task, occurrence history, and reminder worker tests."""

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import database.db as db
from database.m3 import create_reminder, get_reminder
from database.migrations import apply_migrations
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry
from ops.reminder_worker import ReminderWorker


class M8Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_path = Path.cwd() / f".m8-test-{uuid4().hex}.db"
        apply_migrations(self.database_path)
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = self.database_path

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.original_database_path
        self.database_path.unlink(missing_ok=True)

    def test_m8_migration_adds_metadata_without_changing_old_task_shape(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(tasks)")
            }
            self.assertTrue({"source", "request_id"}.issubset(columns))
            reminder_columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(reminders)")
            }
            self.assertIn("source", reminder_columns)
        finally:
            connection.close()

        dispatcher = ToolDispatcher(build_todo_tool_registry())
        context = ToolCallContext(user_id="alice", request_id="m8-create")
        old_result = dispatcher.dispatch("todo_add_task", {"title": "Legacy"}, context)
        new_result = dispatcher.dispatch(
            "task_create",
            {"title": "Unified", "source": "agent", "timezone": "UTC"},
            ToolCallContext(user_id="alice", request_id="m8-unified"),
        )
        self.assertTrue(old_result.ok)
        self.assertTrue(new_result.ok)
        self.assertNotIn("source", old_result.data["data"])
        self.assertEqual(new_result.data["data"]["source"], "agent")
        self.assertEqual(new_result.data["data"]["request_id"], "m8-unified")

    def test_m8_task_actions_are_idempotent_and_write_occurrences(self) -> None:
        dispatcher = ToolDispatcher(build_todo_tool_registry())
        task = dispatcher.dispatch(
            "task_create",
            {"title": "Lifecycle", "timezone": "UTC"},
            ToolCallContext(user_id="alice", request_id="create"),
        ).data["data"]
        context = ToolCallContext(user_id="alice", request_id="same-request")
        started = dispatcher.dispatch(
            "task_transition", {"task_id": task["id"], "action": "start"}, context
        )
        started_again = dispatcher.dispatch(
            "task_transition", {"task_id": task["id"], "action": "start"}, context
        )
        completed = dispatcher.dispatch(
            "task_transition", {"task_id": task["id"], "action": "complete"}, context
        )
        self.assertTrue(started.ok)
        self.assertTrue(started_again.ok)
        self.assertTrue(completed.ok)
        self.assertEqual(started.data["data"]["occurrence"]["id"], started_again.data["data"]["occurrence"]["id"])
        self.assertEqual(completed.data["data"]["task"]["status"], "completed")

        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM task_occurrences WHERE task_id = ?",
                    (task["id"],),
                ).fetchone()[0],
                2,
            )
        finally:
            connection.close()

    def test_m8_history_supports_period_and_status_filters(self) -> None:
        dispatcher = ToolDispatcher(build_todo_tool_registry())
        task = dispatcher.dispatch(
            "task_create",
            {"title": "History", "timezone": "UTC"},
            ToolCallContext(user_id="alice", request_id="history-create"),
        ).data["data"]
        dispatcher.dispatch(
            "task_transition",
            {"task_id": task["id"], "action": "complete", "occurred_at": "2025-01-02T10:00:00Z"},
            ToolCallContext(user_id="alice", request_id="history-complete"),
        )
        history = dispatcher.dispatch(
            "todo_list_occurrences",
            {"task_id": task["id"], "period": "day", "reference_at": "2025-01-02", "status": "completed"},
            ToolCallContext(user_id="alice", request_id="history-read"),
        )
        self.assertTrue(history.ok)
        self.assertEqual(len(history.data["data"]), 1)

    def test_m8_nonrecurring_task_records_each_new_event_with_request_time(self) -> None:
        dispatcher = ToolDispatcher(build_todo_tool_registry())
        task = dispatcher.dispatch(
            "task_create",
            {"title": "Drink yogurt", "timezone": "UTC"},
            ToolCallContext(user_id="alice", request_id="event-create"),
        ).data["data"]

        recurrence = dispatcher.dispatch(
            "recurrence_list",
            {"task_id": task["id"], "active": True},
            ToolCallContext(user_id="alice", request_id="recurrence-check"),
        )
        self.assertTrue(recurrence.ok)
        self.assertEqual(recurrence.data["data"], [])

        first = dispatcher.dispatch(
            "task_transition",
            {
                "task_id": task["id"],
                "action": "complete",
                "occurred_at": "2025-01-01T09:00:00Z",
            },
            ToolCallContext(user_id="alice", request_id="event-one"),
        )
        before_second = datetime.now(timezone.utc)
        second = dispatcher.dispatch(
            "task_transition",
            {"task_id": task["id"], "action": "complete"},
            ToolCallContext(user_id="alice", request_id="event-two"),
        )
        after_second = datetime.now(timezone.utc)

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(second.data["data"]["task"]["status"], "completed")
        self.assertNotEqual(
            first.data["data"]["occurrence"]["id"],
            second.data["data"]["occurrence"]["id"],
        )
        connection = sqlite3.connect(self.database_path)
        try:
            rows = connection.execute(
                "SELECT occurred_at FROM task_occurrences WHERE task_id = ? ORDER BY id",
                (task["id"],),
            ).fetchall()
            self.assertEqual(len(rows), 2)
            second_time = datetime.fromisoformat(rows[1][0].replace("Z", "+00:00"))
            self.assertGreaterEqual(second_time, before_second)
            self.assertLessEqual(second_time, after_second)
        finally:
            connection.close()

    def test_m8_worker_delivers_due_reminder_and_expires_old_reminder(self) -> None:
        task = db.add_task("alice", "Reminder owner")
        due = create_reminder(
            "alice", "Due now", "2025-01-01T10:00:00Z", task_id=task["id"], timezone_name="UTC"
        )
        old = create_reminder(
            "alice", "Too old", "2024-12-01T10:00:00Z", task_id=task["id"], timezone_name="UTC"
        )
        delivered: list[int] = []
        worker = ReminderWorker(delivered.append, expire_after_seconds=86400)
        results = worker.scheduler.run_once(now="2025-01-01T10:00:00Z", limit=10)
        delivered_ids = [item["id"] for item in delivered]
        self.assertIn(due["id"], delivered_ids)
        self.assertNotIn(old["id"], delivered_ids)
        self.assertEqual(get_reminder("alice", due["id"])["status"], "sent")
        self.assertEqual(get_reminder("alice", old["id"])["status"], "expired")


if __name__ == "__main__":
    unittest.main()
