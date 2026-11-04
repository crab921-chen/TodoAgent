import sqlite3
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from database.db import (
    TaskTransactionLinkNotFoundError,
    add_task,
    complete_task,
    create_goal_progress,
    create_task_transaction_link,
    create_transaction,
    delete_task,
    delete_task_transaction_link,
    get_agent_session,
    get_task_by_id,
    get_task_transaction_link,
    get_transaction,
    list_task_transaction_links,
    list_transactions,
    query_statistics,
    set_user_timezone,
    update_transaction,
    void_transaction,
)
from database.m4 import (
    list_tool_audits,
    record_tool_audit,
    upsert_agent_session,
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


class M4DatabaseTests(unittest.TestCase):
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

    def services(self, connection):
        return patch(
            "database.db.get_connection",
            return_value=ConnectionProxy(connection),
        )

    def test_m4_migration_and_constraints_are_idempotent(self):
        with self.temporary_database() as connection:
            first = apply_migrations(connection)
            self.assertEqual(first[-1], "010_m11_exports")
            self.assertEqual(apply_migrations(connection), ())
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {
                    "transactions",
                    "task_transaction_links",
                    "agent_sessions",
                    "tool_audit_logs",
                }.issubset(tables)
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO transactions(user_id, amount_minor, category) VALUES (?, ?, ?)",
                    (999999, 1, "food"),
                )
            connection.execute(
                "INSERT INTO users(external_key) VALUES (?)", ("constraint-user",)
            )
            user_id = connection.execute(
                "SELECT id FROM users WHERE external_key = ?", ("constraint-user",)
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO transactions(user_id, amount_minor, category) VALUES (?, ?, ?)",
                    (user_id, 1.5, "food"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO transactions(user_id, amount_minor, currency, category) VALUES (?, ?, ?, ?)",
                    (user_id, 1, "US1", "food"),
                )

    def test_transaction_crud_uses_minor_units_and_preserves_created_at(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with self.services(connection):
                set_user_timezone("alice", "America/New_York")
                transaction = create_transaction(
                    "alice",
                    "expense",
                    1500,
                    "food",
                    currency="usd",
                    note="Lunch",
                    occurred_at="2025-01-01T00:30:00",
                )
                raw = connection.execute(
                    "SELECT amount_minor, currency, occurred_at, created_at FROM transactions WHERE id = ?",
                    (transaction["id"],),
                ).fetchone()
                self.assertEqual(raw["amount_minor"], 1500)
                self.assertEqual(raw["currency"], "USD")
                self.assertEqual(raw["occurred_at"], "2025-01-01T05:30:00Z")
                created_at = raw["created_at"]
                changed = update_transaction(
                    "alice", transaction["id"], amount_minor=1750, category="dining"
                )
                self.assertEqual(changed["amount_minor"], 1750)
                income = create_transaction("alice", "income", 3000, "salary")
                self.assertEqual(
                    len(list_transactions("alice", transaction_type="EXPENSE", category="dining")),
                    1,
                )
                self.assertEqual(
                    list_transactions("alice", transaction_type="income")[0]["id"],
                    income["id"],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT created_at FROM transactions WHERE id = ?",
                        (transaction["id"],),
                    ).fetchone()["created_at"],
                    created_at,
                )
                with self.assertRaises(ValueError):
                    create_transaction("alice", "expense", 1.5, "invalid")
                with self.assertRaises(ValueError):
                    create_transaction("alice", "refund", 1, "invalid")
                with self.assertRaises(ValueError):
                    create_transaction("alice", "expense", 1, "")
                with self.assertRaises(ValueError):
                    create_transaction(
                        "alice", "expense", 1, "invalid", occurred_at="not-a-date"
                    )
                with self.assertRaises(ValueError):
                    create_transaction("alice", "expense", 9223372036854775808, "invalid")
                self.assertTrue(void_transaction("alice", transaction["id"]))
                self.assertFalse(void_transaction("alice", transaction["id"]))
                self.assertIsNone(
                    update_transaction("alice", transaction["id"], amount_minor=2000)
                )
                self.assertIsNotNone(
                    get_transaction("alice", transaction["id"], include_deleted=True)
                )
                self.assertIsNone(get_transaction("bob", transaction["id"]))

    def test_statistics_use_local_day_boundaries_and_exclude_voided_rows(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with self.services(connection):
                set_user_timezone("alice", "Asia/Shanghai")
                task = add_task("alice", "Daily task")
                goal = create_goal_progress(
                    "alice", task["id"], "2025-01-01", "2025-01-31", 2
                )
                complete_task(
                    "alice",
                    task["id"],
                    occurred_at="2024-12-31T16:00:00Z",
                    idempotency_key="day-1",
                )
                complete_task(
                    "alice",
                    task["id"],
                    occurred_at="2025-01-02T16:00:00Z",
                    idempotency_key="day-2",
                )
                expense = create_transaction(
                    "alice", "expense", 1500, "food", occurred_at="2025-01-01T00:30:00"
                )
                create_transaction(
                    "alice", "expense", 200, "food", currency="USD",
                    occurred_at="2025-01-01T12:00:00",
                )
                create_transaction(
                    "alice", "income", 5000, "salary", occurred_at="2025-01-01T09:00:00"
                )
                stats = query_statistics(
                    "alice",
                    period="custom",
                    range_start="2025-01-01",
                    range_end="2025-01-02",
                )
                self.assertEqual(stats["task_executions"]["total_count"], 1)
                self.assertEqual(stats["completion"]["completion_rate"], 1.0)
                self.assertEqual(stats["goal_progress"][0]["id"], goal["id"])
                self.assertEqual(stats["goal_progress"][0]["completed_count"], 1)
                self.assertEqual(stats["finance"]["expense_total"], {"CNY": 1500, "USD": 200})
                self.assertEqual(stats["finance"]["income_total"], {"CNY": 5000})
                self.assertEqual(len(stats["finance"]["by_category"]), 3)
                filtered = query_statistics(
                    "alice",
                    period="day",
                    reference_at="2025-01-01T12:00:00",
                    transaction_type="EXPENSE",
                    category="food",
                    currency="USD",
                )
                self.assertEqual(filtered["finance"]["transaction_count"], 1)
                self.assertEqual(filtered["finance"]["expense_total"], {"USD": 200})
                timestamp_stats = query_statistics(
                    "alice",
                    range_start="2025-01-01T00:00:00+08:00",
                    range_end="2025-01-02T00:00:00+08:00",
                )
                self.assertEqual(timestamp_stats["finance"]["transaction_count"], 3)
                self.assertTrue(void_transaction("alice", expense["id"]))
                after_void = query_statistics(
                    "alice", period="day", reference_at="2025-01-01T12:00:00"
                )
                self.assertEqual(after_void["finance"]["expense_total"], {"USD": 200})
                self.assertEqual(len(list_transactions("alice")), 2)

    def test_empty_periods_week_month_year_and_dst_return_explicit_zero_results(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with self.services(connection):
                empty = query_statistics(
                    "empty",
                    period="day",
                    reference_at="2025-01-01T12:00:00",
                )
                self.assertEqual(empty["task_executions"]["total_count"], 0)
                self.assertEqual(empty["completion"]["completion_rate"], 0)
                self.assertEqual(empty["finance"]["income_total"], {})
                self.assertEqual(empty["finance"]["expense_total"], {})
                self.assertEqual(empty["goal_progress"], [])
                self.assertEqual(empty["task_executions"]["by_day"], [{"period": "2025-01-01", "count": 0}])
                week = query_statistics(
                    "empty", period="week", reference_at="2025-01-01T12:00:00"
                )
                month = query_statistics(
                    "empty", period="month", reference_at="2025-01-01T12:00:00"
                )
                year = query_statistics(
                    "empty", period="year", reference_at="2025-01-01T12:00:00"
                )
                self.assertEqual(week["range_start"], "2024-12-30 00:00:00")
                self.assertEqual(month["range_end"], "2025-02-01 00:00:00")
                self.assertEqual(year["range_end"], "2026-01-01 00:00:00")
                set_user_timezone("empty", "America/New_York")
                dst = query_statistics(
                    "empty", period="day", reference_at="2025-03-09T12:00:00"
                )
                self.assertEqual(dst["utc_range_start"], "2025-03-09T05:00:00Z")
                self.assertEqual(dst["utc_range_end"], "2025-03-10T04:00:00Z")

    def test_explicit_task_link_is_user_scoped_unique_and_does_not_complete_task(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with self.services(connection):
                task = add_task("alice", "Buy supplies")
                transaction = create_transaction("alice", "expense", 2500, "supplies")
                link = create_task_transaction_link(
                    "alice", task["id"], transaction["id"], basis="explicit"
                )
                self.assertEqual(link["basis"], "explicit")
                self.assertEqual(get_task_by_id("alice", task["id"])["status"], "pending")
                with self.assertRaises(ValueError):
                    create_task_transaction_link(
                        "alice", task["id"], transaction["id"], basis="explicit"
                    )
                with self.assertRaises(ValueError):
                    create_task_transaction_link(
                        "alice", task["id"], transaction["id"], basis="inferred"
                    )
                self.assertIsNone(get_task_transaction_link("bob", link["id"]))
                self.assertEqual(
                    list_task_transaction_links("alice", task_id=task["id"])[0]["id"],
                    link["id"],
                )
                with self.assertRaises(TaskTransactionLinkNotFoundError):
                    create_task_transaction_link("bob", task["id"], transaction["id"])
                self.assertTrue(delete_task("alice", task["id"]))
                self.assertIsNotNone(get_task_transaction_link("alice", link["id"]))
                self.assertTrue(delete_task_transaction_link("alice", link["id"]))

    def test_sessions_and_audit_store_metadata_without_raw_arguments(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with self.services(connection):
                session = upsert_agent_session(
                    "alice", "session-1", request_id="request-1"
                )
                self.assertEqual(session["session_key"], "session-1")
                updated_session = upsert_agent_session(
                    "alice", "session-1", request_id="request-2"
                )
                self.assertEqual(updated_session["id"], session["id"])
                self.assertEqual(get_agent_session("alice", "session-1")["last_request_id"], "request-2")
                record_tool_audit(
                    "alice",
                    request_id="request-1",
                    tool_name="transaction_create",
                    session_key="session-1",
                    input_digest="digest",
                    args_summary={"amount_minor": "1500"},
                    success=True,
                    status="success",
                    duration_ms=3,
                )
                audit = list_tool_audits("alice", request_id="request-1")[0]
                self.assertEqual(audit["success"], 1)
                self.assertEqual(audit["input_digest"], "digest")
                self.assertNotIn("1500", audit["args_summary"])
                record_tool_audit(
                    "alice",
                    request_id="request-failed",
                    tool_name="transaction_update",
                    success=False,
                    status="failed",
                    error_code="TRANSACTION_NOT_FOUND",
                )
                self.assertEqual(
                    len(list_tool_audits("bob", request_id="request-1")), 0
                )
                self.assertEqual(
                    list_tool_audits("alice", request_id="request-failed")[0]["success"],
                    0,
                )

    def test_harness_exposes_m4_tools_and_confirmation(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with self.services(connection):
                dispatcher = ToolDispatcher(
                    build_todo_tool_registry(),
                    authorization=PermissionPolicy(
                        confirmation_callback=lambda context, call, spec: True
                    ),
                )
                context = ToolCallContext(user_id="alice", request_id="m4-tools")
                task = dispatcher.dispatch("todo_add_task", {"title": "Tool task"}, context)
                transaction = dispatcher.dispatch(
                    "transaction_create",
                    {
                        "type": "expense",
                        "amount_minor": 900,
                        "category": "tools",
                    },
                    context,
                )
                self.assertTrue(transaction.ok)
                stats = dispatcher.dispatch(
                    "statistics_query",
                    {
                        "period": "custom",
                        "range_start": "2025-01-01",
                        "range_end": "2025-01-02",
                    },
                    context,
                )
                self.assertTrue(stats.ok)
                link = dispatcher.dispatch(
                    "task_transaction_link",
                    {
                        "task_id": task.data["data"]["id"],
                        "transaction_id": transaction.data["data"]["id"],
                        "basis": "user_confirmed",
                    },
                    context,
                )
                self.assertTrue(link.ok)
                denied = ToolDispatcher(build_todo_tool_registry()).dispatch(
                    "transaction_void",
                    {"transaction_id": transaction.data["data"]["id"]},
                    context,
                )
                self.assertFalse(denied.ok)
                self.assertEqual(denied.error.code, "CONFIRMATION_REQUIRED")
                bob = ToolCallContext(user_id="bob", request_id="m4-bob")
                bob_update = dispatcher.dispatch(
                    "transaction_update",
                    {"transaction_id": transaction.data["data"]["id"], "amount_minor": 901},
                    bob,
                )
                self.assertFalse(bob_update.ok)
                self.assertEqual(bob_update.error.code, "TRANSACTION_NOT_FOUND")
                bob_link = dispatcher.dispatch(
                    "task_transaction_link_get",
                    {"link_id": link.data["data"]["id"]},
                    bob,
                )
                self.assertFalse(bob_link.ok)
                self.assertEqual(bob_link.error.code, "LINK_NOT_FOUND")
                deleted = dispatcher.dispatch(
                    "transaction_delete",
                    {"transaction_id": transaction.data["data"]["id"]},
                    context,
                )
                self.assertTrue(deleted.ok)
                audits = list_tool_audits("alice", request_id="m4-tools")
                self.assertGreaterEqual(len(audits), 3)
                self.assertTrue(all(item["input_digest"] for item in audits))


if __name__ == "__main__":
    unittest.main()
