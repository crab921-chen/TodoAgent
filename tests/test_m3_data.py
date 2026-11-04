import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from database.db import (
    add_task,
    cancel_reminder,
    claim_due_reminders,
    create_goal_progress,
    create_recurrence_rule,
    create_reminder,
    dispatch_due_reminders,
    expire_due_reminders,
    get_goal_progress,
    get_reminder,
    get_recurrence_rule,
    generate_recurrence_instances,
    list_goal_progress,
    list_recurrence_rules,
    list_reminders,
    materialize_recurrence_reminders,
    mark_reminder_failed,
    mark_reminder_sent,
    recover_stale_reminders,
    ReminderScheduler,
    RecurrenceNotFoundError,
    complete_task,
    record_task_occurrence,
    update_recurrence_rule,
    update_reminder,
)
from database.migrations import apply_migrations
from harness.security import PermissionPolicy
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry


class ConnectionProxy:
    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        pass


class M3DatabaseTests(unittest.TestCase):
    def temporary_database(self):
        @contextmanager
        def database_context():
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            try:
                yield connection
            finally:
                connection.close()

        return database_context()

    def test_m3_schema_is_migrated_and_idempotent(self):
        with self.temporary_database() as connection:
            first = apply_migrations(connection)
            second = apply_migrations(connection)
            self.assertIn("004_reminders_goal", first)
            self.assertEqual(second, ())
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {"reminders", "recurrence_rules", "goal_progress"}.issubset(tables)
            )

    def test_m3_reminder_crud_and_user_isolation(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Reminder task")
                reminder = create_reminder(
                    "alice",
                    "Review the release",
                    "2025-01-01T09:00:00",
                    task_id=task["id"],
                    timezone_name="Asia/Shanghai",
                )
                raw = connection.execute(
                    "SELECT trigger_at, timezone, status FROM reminders WHERE id = ?",
                    (reminder["id"],),
                ).fetchone()
                self.assertEqual(raw["trigger_at"], "2025-01-01T01:00:00Z")
                self.assertEqual(raw["timezone"], "Asia/Shanghai")
                self.assertEqual(raw["status"], "scheduled")
                self.assertEqual(get_reminder("bob", reminder["id"]), None)
                changed = update_reminder(
                    "alice", reminder["id"], content="Review the final release"
                )
                self.assertEqual(changed["content"], "Review the final release")
                self.assertEqual(len(list_reminders("alice", status="scheduled")), 1)

                create_reminder("alice", "Second", "2025-01-02T09:00:00Z", timezone_name="UTC")
                create_reminder("alice", "Third", "2025-01-03T09:00:00Z", timezone_name="UTC")
                page = list_reminders("alice", limit=2, offset=1)
                self.assertEqual([item["content"] for item in page], ["Second", "Third"])

    def test_m3_reminder_update_recalculates_utc_and_rejects_invalid_state(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                reminder = create_reminder(
                    "alice",
                    "Move reminder",
                    "2025-01-01T09:00:00",
                    timezone_name="Asia/Shanghai",
                )
                changed = update_reminder(
                    "alice",
                    reminder["id"],
                    trigger_at="2025-01-02T09:30:00",
                    timezone_name="America/New_York",
                )
                self.assertEqual(changed["timezone"], "America/New_York")
                raw = connection.execute(
                    "SELECT trigger_at FROM reminders WHERE id = ?",
                    (reminder["id"],),
                ).fetchone()
                self.assertEqual(raw["trigger_at"], "2025-01-02T14:30:00Z")
                with self.assertRaises(ValueError):
                    update_reminder(
                        "alice", reminder["id"], timezone_name="Invalid/Timezone"
                    )
                self.assertEqual(
                    connection.execute(
                        "SELECT trigger_at FROM reminders WHERE id = ?",
                        (reminder["id"],),
                    ).fetchone()["trigger_at"],
                    "2025-01-02T14:30:00Z",
                )
                claim_due_reminders("alice", now="2025-01-02T14:30:00Z")
                with self.assertRaises(ValueError):
                    update_reminder("alice", reminder["id"], content="late edit")

    def test_m3_claim_supports_batch_limits_and_retry_chain(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                reminders = [
                    create_reminder(
                        "alice", f"Batch {index}", "2025-01-01T10:00:00Z", timezone_name="UTC"
                    )
                    for index in range(3)
                ]
                first = claim_due_reminders("alice", now="2025-01-01T10:00:00Z", limit=2)
                self.assertEqual(len(first), 2)
                self.assertEqual(
                    len(claim_due_reminders("alice", now="2025-01-01T10:00:01Z", limit=2)), 1
                )
                mark_reminder_failed(
                    "alice", reminders[0]["id"], "temporary", max_attempts=2,
                    retry_delay_seconds=30, failed_at="2025-01-01T10:00:01Z"
                )
                self.assertEqual(
                    len(claim_due_reminders("alice", now="2025-01-01T10:00:31Z")), 1
                )
                mark_reminder_failed(
                    "alice", reminders[0]["id"], "final", max_attempts=2,
                    failed_at="2025-01-01T10:00:31Z"
                )
                self.assertEqual(
                    claim_due_reminders(
                        "alice", now="2025-01-01T11:00:00Z", stale_after_seconds=3600
                    ),
                    [],
                )

    def test_m3_expire_and_recover_have_strict_time_boundaries(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                reminder = create_reminder("alice", "Boundary", "2025-01-01T10:00:00Z", timezone_name="UTC")
                self.assertEqual(
                    expire_due_reminders(
                        now="2025-01-02T10:00:00Z", max_age_seconds=86400
                    ),
                    0,
                )
                self.assertEqual(
                    expire_due_reminders(
                        now="2025-01-02T10:00:01Z", max_age_seconds=86400
                    ),
                    1,
                )
                processing = create_reminder("alice", "Lease", "2025-01-01T10:00:00Z", timezone_name="UTC")
                claim_due_reminders("alice", now="2025-01-01T10:00:00Z")
                connection.execute(
                    "UPDATE reminders SET updated_at = '2025-01-01T09:59:00Z' WHERE id = ?",
                    (processing["id"],),
                )
                connection.commit()
                self.assertEqual(
                    recover_stale_reminders(
                        now="2025-01-01T10:00:00Z", stale_after_seconds=60
                    ),
                    0,
                )
                self.assertEqual(
                    recover_stale_reminders(
                        now="2025-01-01T10:00:01Z", stale_after_seconds=60
                    ),
                    1,
                )

    def test_m3_task_delete_cascades_bound_reminders(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Cascade task")
                reminder = create_reminder(
                    "alice", "Cascade", "2025-01-01T10:00:00Z", task_id=task["id"], timezone_name="UTC"
                )
                user_pk = connection.execute(
                    "SELECT id FROM users WHERE external_key = 'alice'"
                ).fetchone()[0]
                connection.execute(
                    "DELETE FROM tasks WHERE id = ? AND user_id = ?",
                    (task["id"], user_pk),
                )
                connection.commit()
                self.assertIsNone(get_reminder("alice", reminder["id"]))

    def test_m3_claim_is_atomic_and_send_is_idempotent(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                reminder = create_reminder(
                    "alice", "Due now", "2025-01-01T10:00:00Z", timezone_name="UTC"
                )
                self.assertEqual(
                    claim_due_reminders("alice", now="2025-01-01T09:59:59Z"), []
                )
                claimed = claim_due_reminders("alice", now="2025-01-01T10:00:00Z")
                self.assertEqual([item["id"] for item in claimed], [reminder["id"]])
                self.assertEqual(
                    claim_due_reminders("alice", now="2025-01-01T10:00:01Z"), []
                )
                self.assertTrue(
                    mark_reminder_sent(
                        "alice", reminder["id"], sent_at="2025-01-01T10:00:02Z"
                    )
                )
                self.assertFalse(mark_reminder_sent("alice", reminder["id"]))
                self.assertEqual(get_reminder("alice", reminder["id"])["status"], "sent")

    def test_m3_cancellation_during_delivery_cannot_report_sent(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                reminder = create_reminder(
                    "alice", "Race", "2025-01-01T10:00:00Z", timezone_name="UTC"
                )

                def cancel_while_sending(item):
                    self.assertTrue(cancel_reminder("alice", item["id"]))

                result = dispatch_due_reminders(
                    cancel_while_sending,
                    user_id="alice",
                    now="2025-01-01T10:00:00Z",
                )
                self.assertEqual(result[0]["status"], "cancelled")
                self.assertEqual(get_reminder("alice", reminder["id"])["status"], "cancelled")

    def test_m3_failure_retry_limit_and_restart_recovery(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                retry = create_reminder("alice", "Retry", "2025-01-01T10:00:00Z", timezone_name="UTC")
                claim_due_reminders("alice", now="2025-01-01T10:00:00Z")
                mark_reminder_failed(
                    "alice", retry["id"], "temporary", max_attempts=2,
                    retry_delay_seconds=60, failed_at="2025-01-01T10:00:00Z"
                )
                self.assertEqual(
                    claim_due_reminders("alice", now="2025-01-01T10:00:30Z"), []
                )
                claim_due_reminders("alice", now="2025-01-01T10:01:00Z")
                mark_reminder_failed(
                    "alice", retry["id"], "permanent", max_attempts=2,
                    retry_delay_seconds=60, failed_at="2025-01-01T10:01:00Z"
                )
                failed = get_reminder("alice", retry["id"])
                self.assertEqual(failed["status"], "failed")
                self.assertIsNone(failed["next_retry_at"])

                restart = create_reminder("alice", "Restart", "2025-01-01T10:00:00Z", timezone_name="UTC")
                claim_due_reminders("alice", now="2025-01-01T10:02:00Z")
                connection.execute(
                    "UPDATE reminders SET updated_at = '2025-01-01T09:00:00Z' WHERE id = ?",
                    (restart["id"],),
                )
                connection.commit()
                self.assertEqual(
                    recover_stale_reminders(now="2025-01-01T10:03:00Z", stale_after_seconds=60),
                    1,
                )
                self.assertEqual(
                    len(claim_due_reminders("alice", now="2025-01-01T10:03:01Z")), 1
                )

    def test_m3_scheduler_facade_recovers_and_dispatches_one_round(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                reminder = create_reminder(
                    "alice", "Run scheduler", "2025-01-01T10:00:00Z", timezone_name="UTC"
                )
                delivered = []
                scheduler = ReminderScheduler(lambda item: delivered.append(item["id"]))
                result = scheduler.run_once(now="2025-01-01T10:00:00Z")
                self.assertEqual(delivered, [reminder["id"]])
                self.assertEqual(result[0]["status"], "sent")
                self.assertEqual(get_reminder("alice", reminder["id"])["status"], "sent")

    def test_m3_cancel_and_expire_prevent_delivery(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                cancelled = create_reminder("alice", "Cancel", "2025-01-01T10:00:00Z", timezone_name="UTC")
                self.assertTrue(cancel_reminder("alice", cancelled["id"]))
                self.assertEqual(claim_due_reminders("alice", now="2025-01-02T10:00:00Z"), [])
                expired = create_reminder("alice", "Expire", "2025-01-01T10:00:00Z", timezone_name="UTC")
                self.assertEqual(
                    expire_due_reminders(now="2025-01-03T10:00:00Z", max_age_seconds=3600), 1
                )
                self.assertEqual(get_reminder("alice", expired["id"])["status"], "expired")

    def test_m3_recurrence_generates_independent_calendar_instances(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Practice")
                rule = create_recurrence_rule(
                    "alice", task["id"], "daily", "2025-01-01",
                    interval_value=2, end_date="2025-01-07", timezone_name="UTC",
                )
                instances = generate_recurrence_instances(
                    "alice", rule["id"], range_start="2025-01-01", range_end="2025-01-07"
                )
                self.assertEqual(
                    [item["instance_date"] for item in instances],
                    ["2025-01-01", "2025-01-03", "2025-01-05", "2025-01-07"],
                )
                self.assertNotEqual(instances[0]["idempotency_key"], instances[1]["idempotency_key"])

    def test_m3_recurrence_crud_is_user_scoped_and_supports_calendar_boundaries(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                alice_task = add_task("alice", "Monthly")
                add_task("bob", "Other")
                rule = create_recurrence_rule(
                    "alice", alice_task["id"], "monthly", "2025-01-31",
                    end_date="2025-05-31", timezone_name="America/New_York",
                )
                self.assertIsNone(get_recurrence_rule("bob", rule["id"]))
                self.assertEqual(len(list_recurrence_rules("bob")), 0)
                with self.assertRaises(RecurrenceNotFoundError):
                    update_recurrence_rule("bob", rule["id"], active=False)
                updated = update_recurrence_rule(
                    "alice", rule["id"], frequency="weekly", interval_value=2
                )
                self.assertEqual(updated["frequency"], "weekly")
                self.assertEqual(updated["interval_value"], 2)
                self.assertEqual(
                    [item["instance_date"] for item in generate_recurrence_instances(
                        "alice", rule["id"], range_start="2025-02-01", range_end="2025-03-01"
                    )],
                    ["2025-02-14", "2025-02-28"],
                )
                monthly = create_recurrence_rule(
                    "alice", alice_task["id"], "monthly", "2025-01-31",
                    end_date="2025-05-31", timezone_name="UTC",
                )
                self.assertEqual(
                    [item["instance_date"] for item in generate_recurrence_instances(
                        "alice", monthly["id"], range_start="2025-01-01", range_end="2025-05-31"
                    )],
                    ["2025-01-31", "2025-02-28", "2025-03-31", "2025-04-30", "2025-05-31"],
                )
                paused = update_recurrence_rule("alice", rule["id"], active=False)
                self.assertEqual(generate_recurrence_instances("alice", rule["id"]), [])
                self.assertEqual(paused["active"], 0)

                open_rule = create_recurrence_rule(
                    "alice", alice_task["id"], "custom", "2025-06-01",
                    timezone_name="UTC",
                )
                self.assertEqual(
                    [item["instance_date"] for item in generate_recurrence_instances(
                        "alice", open_rule["id"],
                        range_start="2025-06-01", range_end="2025-06-05",
                    )],
                    [
                        "2025-06-01",
                        "2025-06-02",
                        "2025-06-03",
                        "2025-06-04",
                        "2025-06-05",
                    ],
                )

    def test_m3_recurrence_handles_dst_and_explicit_ranges(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "DST")
                rule = create_recurrence_rule(
                    "alice", task["id"], "weekly", "2025-03-02",
                    end_date="2025-03-30", timezone_name="America/New_York",
                )
                instances = generate_recurrence_instances(
                    "alice", rule["id"], range_start="2025-03-09", range_end="2025-03-16"
                )
                self.assertEqual(
                    [item["instance_date"] for item in instances],
                    ["2025-03-09", "2025-03-16"],
                )
                reminders = materialize_recurrence_reminders(
                    "alice", rule["id"], range_start="2025-03-09", range_end="2025-03-16",
                    local_time="09:00",
                )
                self.assertEqual(
                    [item["trigger_at"] for item in reminders],
                    ["2025-03-09 09:00:00 EDT", "2025-03-16 09:00:00 EDT"],
                )

    def test_m3_goal_counts_only_valid_occurrences_and_completes_at_target(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Drink water")
                goal = create_goal_progress(
                    "alice", task["id"], "2025-01-01", "2025-01-31", 2
                )
                complete_task("alice", task["id"], occurred_at="2025-01-05T10:00:00Z", idempotency_key="g-1")
                self.assertEqual(get_goal_progress("alice", goal["id"])["completed_count"], 1)
                complete_task("alice", task["id"], occurred_at="2025-01-06T10:00:00Z", idempotency_key="g-2")
                progress = get_goal_progress("alice", goal["id"])
                self.assertEqual(progress["completed_count"], 2)
                self.assertEqual(progress["status"], "completed")
                complete_task("alice", task["id"], occurred_at="2025-01-06T10:00:00Z", idempotency_key="g-2")
                self.assertEqual(get_goal_progress("alice", goal["id"])["completed_count"], 2)

    def test_m3_goal_excludes_occurrences_outside_window_and_non_completed_status(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Windowed goal")
                goal = create_goal_progress(
                    "alice", task["id"], "2025-01-10", "2025-01-20", 2
                )
                complete_task("alice", task["id"], occurred_at="2025-01-09T10:00:00Z", idempotency_key="before")
                complete_task("alice", task["id"], occurred_at="2025-01-10T10:00:00Z", idempotency_key="inside")
                record_task_occurrence(
                    "alice", task["id"], status="started",
                    occurred_at="2025-01-11T10:00:00Z", idempotency_key="started"
                )
                complete_task("alice", task["id"], occurred_at="2025-01-20T10:00:00Z", idempotency_key="end")
                complete_task("alice", task["id"], occurred_at="2025-01-21T10:00:00Z", idempotency_key="after")
                progress = get_goal_progress("alice", goal["id"])
                self.assertEqual(progress["completed_count"], 2)

    def test_m3_goal_tools_enforce_cross_user_reads(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                alice_task = add_task("alice", "Alice goal")
                goal = create_goal_progress(
                    "alice", alice_task["id"], "2025-01-01", "2025-01-31", 1
                )
                self.assertEqual(list_goal_progress("bob"), [])
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                result = dispatcher.dispatch(
                    "goal_progress_get",
                    {"goal_id": goal["id"]},
                    ToolCallContext(user_id="bob", request_id="m3-goal-bob"),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, "GOAL_NOT_FOUND")

    def test_m3_harness_exposes_reminder_recurrence_and_goal_tools(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                dispatcher = ToolDispatcher(
                    build_todo_tool_registry(),
                    authorization=PermissionPolicy(
                        confirmation_callback=lambda context, call, spec: True
                    ),
                )
                context = ToolCallContext(user_id="alice", request_id="m3-tools")
                task = dispatcher.dispatch("todo_add_task", {"title": "Tool task"}, context)
                task_id = task.data["data"]["id"]
                reminder = dispatcher.dispatch(
                    "reminder_create",
                    {"content": "Do it", "trigger_at": "2025-01-01T10:00:00Z", "task_id": task_id, "timezone": "UTC"},
                    context,
                )
                self.assertTrue(reminder.ok)
                goal = dispatcher.dispatch(
                    "goal_progress_create",
                    {"task_id": task_id, "period_start": "2025-01-01", "period_end": "2025-01-31", "target_count": 1},
                    context,
                )
                self.assertTrue(goal.ok)
                listed = dispatcher.dispatch("reminder_list", {}, context)
                self.assertTrue(listed.ok)
                self.assertEqual(len(listed.data["data"]), 1)
                self.assertEqual(
                    dispatcher.dispatch(
                        "reminder_cancel",
                        {"reminder_id": reminder.data["data"]["id"]},
                        ToolCallContext(user_id="bob", request_id="m3-bob"),
                    ).error.code,
                    "REMINDER_NOT_FOUND",
                )
                updated_reminder = dispatcher.dispatch(
                    "reminder_update",
                    {
                        "reminder_id": reminder.data["data"]["id"],
                        "content": "Updated reminder",
                        "trigger_at": "2025-01-02T10:00:00Z",
                        "timezone": "UTC",
                    },
                    context,
                )
                self.assertTrue(updated_reminder.ok)
                recurrence = dispatcher.dispatch(
                    "recurrence_create",
                    {
                        "task_id": task_id,
                        "frequency": "daily",
                        "start_date": "2025-01-01",
                        "end_date": "2025-01-03",
                    },
                    context,
                )
                self.assertTrue(recurrence.ok)
                recurrence_update = dispatcher.dispatch(
                    "recurrence_update",
                    {"rule_id": recurrence.data["data"]["id"], "active": False},
                    context,
                )
                self.assertTrue(recurrence_update.ok)
                cross_user_recurrence = dispatcher.dispatch(
                    "recurrence_update",
                    {"rule_id": recurrence.data["data"]["id"], "active": True},
                    ToolCallContext(user_id="bob", request_id="m3-recurrence-bob"),
                )
                self.assertFalse(cross_user_recurrence.ok)
                self.assertEqual(cross_user_recurrence.error.code, "RECURRENCE_NOT_FOUND")
                goal_result = dispatcher.dispatch(
                    "goal_progress_get",
                    {"goal_id": goal.data["data"]["id"]},
                    context,
                )
                self.assertTrue(goal_result.ok)


if __name__ == "__main__":
    unittest.main()
