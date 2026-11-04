import sqlite3
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import patch

from database.db import (
    InvalidTaskStatusError,
    InvalidTimestampError,
    TaskNotFoundError,
    add_task,
    complete_task,
    convert_utc_to_local,
    delete_task,
    get_all_tasks,
    get_task_by_id,
    get_task_occurrences,
    record_task_occurrence,
    search_tasks,
    get_user_timezone,
    set_user_timezone,
    update_task,
)
from database.migrations import MigrationDefinition, MigrationError, apply_migrations
from harness.security import PermissionPolicy
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry


class ConnectionProxy:
    """Keep one in-memory connection alive across service calls."""

    def __init__(self, connection):
        self._connection = connection

    def __getattr__(self, name):
        return getattr(self._connection, name)

    def close(self):
        pass


class M2DatabaseTests(unittest.TestCase):
    def temporary_database(self):
        @contextmanager
        def database_context():
            connection = sqlite3.connect(":memory:", check_same_thread=False)
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                yield connection
            finally:
                connection.close()

        return database_context()

    def test_m2_01_fresh_migrations_are_idempotent(self):
        with self.temporary_database() as connection:
            first = apply_migrations(connection)
            second = apply_migrations(connection)

            self.assertEqual(
                first,
                (
                    "001_initial",
                    "002_occurrences",
                    "003_occurrence_metadata",
                    "004_reminders_goal",
                    "005_finance_audit",
                    "006_conversations",
                    "007_m8_task_reminder_metadata",
                    "008_m9_plans",
                    "009_m10_charts",
                    "010_m11_exports",
                ),
            )
            self.assertEqual(second, ())
            tables = {
                row["name"]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            versions = [
                row["version"]
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                )
            ]

            self.assertTrue({
                "users",
                "user_profiles",
                "tasks",
                "task_occurrences",
                "reminders",
                "recurrence_rules",
                "goal_progress",
                "schema_migrations",
            }.issubset(tables))
            self.assertEqual(
                versions,
                [
                    "001_initial",
                    "002_occurrences",
                    "003_occurrence_metadata",
                    "004_reminders_goal",
                    "005_finance_audit",
                    "006_conversations",
                    "007_m8_task_reminder_metadata",
                    "008_m9_plans",
                    "009_m10_charts",
                    "010_m11_exports",
                ],
            )

    def test_m2_01_enables_foreign_keys_and_rejects_orphan_rows(self):
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            apply_migrations(connection)

            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO tasks(user_id, title) VALUES (?, ?)",
                    (999999, "orphan"),
                )
        finally:
            connection.close()

    def test_m2_01_failed_migration_rolls_back_all_changes(self):
        def failing_migration(connection):
            connection.execute(
                "CREATE TABLE partial_migration (id INTEGER PRIMARY KEY)"
            )
            raise RuntimeError("migration failure")

        migration = MigrationDefinition(
            version="999_failure",
            source="rollback-test",
            apply=failing_migration,
        )
        with self.temporary_database() as connection:
            with patch("database.migrations.MIGRATIONS", (migration,)):
                with self.assertRaises(MigrationError):
                    apply_migrations(connection)

            table = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("partial_migration",),
            ).fetchone()
            applied = connection.execute(
                "SELECT version FROM schema_migrations WHERE version = ?",
                ("999_failure",),
            ).fetchone()
            self.assertIsNone(table)
            self.assertIsNone(applied)

    def test_m2_01_occurrence_and_task_update_roll_back_together_on_database_failure(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Rollback execution")
                connection.execute(
                    """
                    CREATE TRIGGER reject_task_completion
                    BEFORE UPDATE OF status ON tasks
                    WHEN NEW.status = 'completed'
                    BEGIN
                        SELECT RAISE(ABORT, 'forced task update failure');
                    END
                    """
                )

                with self.assertRaises(sqlite3.IntegrityError):
                    record_task_occurrence(
                        "alice",
                        task["id"],
                        status="completed",
                        idempotency_key="rollback-1",
                    )

                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM task_occurrences").fetchone()[0],
                    0,
                )
                self.assertEqual(get_task_by_id("alice", task["id"])["status"], "pending")

    def test_m2_01_migrates_legacy_tasks_without_losing_rows(self):
        with self.temporary_database() as connection:
            connection.execute(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                "INSERT INTO tasks(user_id, title, description, status) VALUES (?, ?, ?, ?)",
                ("alice", "Existing", "Keep me", "completed"),
            )
            connection.commit()
            apply_migrations(connection)
            task = connection.execute(
                "SELECT tasks.*, users.external_key FROM tasks JOIN users ON users.id = tasks.user_id"
            ).fetchone()
            profile_count = connection.execute(
                "SELECT COUNT(*) AS count FROM user_profiles"
            ).fetchone()["count"]

            self.assertEqual(task["external_key"], "alice")
            self.assertEqual(task["title"], "Existing")
            self.assertEqual(task["status"], "completed")
            self.assertEqual(profile_count, 1)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM task_occurrences").fetchone()[0],
                0,
            )

    def test_m2_01_migrates_multiple_legacy_rows_and_deduplicates_users(self):
        with self.temporary_database() as connection:
            connection.execute(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO tasks(user_id, title, description, status)
                VALUES (?, ?, ?, ?)
                """,
                [
                    ("alice", "First", None, "pending"),
                    ("alice", "Second", "Keep", "completed"),
                    ("bob", "Third", None, "cancelled"),
                ],
            )
            connection.commit()

            apply_migrations(connection)

            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM users").fetchone()[0],
                2,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0],
                2,
            )
            migrated = connection.execute(
                """
                SELECT users.external_key, tasks.title, tasks.status
                FROM tasks JOIN users ON users.id = tasks.user_id
                ORDER BY tasks.id
                """
            ).fetchall()
            self.assertEqual(
                [(row[0], row[1], row[2]) for row in migrated],
                [
                    ("alice", "First", "pending"),
                    ("alice", "Second", "completed"),
                    ("bob", "Third", "cancelled"),
                ],
            )

    def test_m2_02_crud_supports_same_titles_pagination_and_user_isolation(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                first = add_task("alice", "Same title")
                second = add_task("alice", "Same title")
                add_task("alice", "Other title")
                add_task("bob", "Same title")

                self.assertNotEqual(first["id"], second["id"])
                self.assertEqual(len(get_all_tasks("alice", limit=2)), 2)
                self.assertEqual(len(get_all_tasks("alice", limit=2, offset=2)), 1)
                self.assertEqual(len(search_tasks("alice", "Same title")), 2)
                self.assertEqual(get_all_tasks("bob")[0]["user_id"], "bob")
                self.assertIsNone(get_task_by_id("bob", first["id"]))

                changed = update_task("alice", first["id"], title="Changed")
                self.assertEqual(changed["title"], "Changed")
                self.assertEqual(get_task_by_id("alice", second["id"])["title"], "Same title")

    def test_m2_02_cross_user_update_and_delete_do_not_mutate_task(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Alice task")
                add_task("bob", "Bob task")

                self.assertIsNone(update_task("bob", task["id"], title="Hijacked"))
                self.assertFalse(delete_task("bob", task["id"]))
                self.assertEqual(get_task_by_id("alice", task["id"])["title"], "Alice task")
                self.assertEqual(len(get_all_tasks("alice")), 1)

    def test_m2_02_occurrences_enforce_task_user_foreign_keys_and_idempotency(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            connection.execute("INSERT INTO users(external_key) VALUES (?)", ("alice",))
            connection.execute("INSERT INTO users(external_key) VALUES (?)", ("bob",))
            users = {
                row["external_key"]: row["id"]
                for row in connection.execute(
                    "SELECT id, external_key FROM users"
                )
            }
            cursor = connection.execute(
                "INSERT INTO tasks(user_id, title) VALUES (?, ?)",
                (users["alice"], "Run me"),
            )
            task_id = cursor.lastrowid

            connection.execute(
                """
                INSERT INTO task_occurrences(task_id, user_id, idempotency_key)
                VALUES (?, ?, ?)
                """,
                (task_id, users["alice"], "run-1"),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO task_occurrences(task_id, user_id, idempotency_key)
                    VALUES (?, ?, ?)
                    """,
                    (task_id, users["alice"], "run-1"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO task_occurrences(task_id, user_id, idempotency_key)
                    VALUES (?, ?, ?)
                    """,
                    (task_id, users["bob"], "wrong-owner"),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO task_occurrences(task_id, user_id, idempotency_key)
                    VALUES (?, ?, ?)
                    """,
                    (999999, users["alice"], "missing-task"),
                )

            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM task_occurrences").fetchone()[0],
                1,
            )

    def test_m2_03_enforces_task_state_machine(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Stateful")
                task = update_task("alice", task["id"], status="in_progress")
                self.assertEqual(task["status"], "in_progress")
                task = update_task("alice", task["id"], status="completed")
                self.assertEqual(task["status"], "completed")
                task = update_task("alice", task["id"], status="pending")
                self.assertEqual(task["status"], "pending")

                with self.assertRaises(InvalidTaskStatusError):
                    update_task("alice", task["id"], status="archived")
                with self.assertRaises(InvalidTaskStatusError):
                    update_task("alice", task["id"], status="unknown")

    def test_m2_03_covers_allowed_and_forbidden_status_transitions(self):
        allowed = {
            "pending": {"in_progress", "completed", "cancelled"},
            "in_progress": {"completed", "pending", "cancelled"},
            "completed": {"pending", "in_progress"},
            "cancelled": {"archived"},
        }
        paths = {
            "pending": (),
            "in_progress": ("in_progress",),
            "completed": ("completed",),
            "cancelled": ("cancelled",),
        }

        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                for source, targets in allowed.items():
                    for target in targets:
                        task = add_task("alice", f"{source}-{target}")
                        for intermediate in paths[source]:
                            update_task("alice", task["id"], status=intermediate)
                        result = update_task("alice", task["id"], status=target)
                        if target == "archived":
                            self.assertIsNone(result)
                            raw = connection.execute(
                                "SELECT status, deleted_at FROM tasks WHERE id = ?",
                                (task["id"],),
                            ).fetchone()
                            self.assertEqual(raw["status"], "archived")
                            self.assertIsNotNone(raw["deleted_at"])
                            self.assertIsNone(
                                update_task("alice", task["id"], status="pending")
                            )
                            raw_after_attempt = connection.execute(
                                "SELECT status FROM tasks WHERE id = ?",
                                (task["id"],),
                            ).fetchone()
                            self.assertEqual(raw_after_attempt["status"], "archived")
                        else:
                            self.assertEqual(result["status"], target)

                forbidden = {
                    "pending": {"archived"},
                    "in_progress": {"archived"},
                    "completed": {"cancelled", "archived"},
                    "cancelled": {"pending", "in_progress", "completed"},
                }
                for source, targets in forbidden.items():
                    for target in targets:
                        task = add_task("alice", f"forbidden-{source}-{target}")
                        for intermediate in paths[source]:
                            update_task("alice", task["id"], status=intermediate)
                        with self.assertRaises(InvalidTaskStatusError):
                            update_task("alice", task["id"], status=target)

    def test_m2_03_delete_uses_soft_archive_after_cancel(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Archive me")
                task = update_task("alice", task["id"], status="cancelled")
                self.assertEqual(task["status"], "cancelled")
                self.assertTrue(delete_task("alice", task["id"]))
                self.assertIsNone(get_task_by_id("alice", task["id"]))

                row = connection.execute(
                    "SELECT status, deleted_at FROM tasks WHERE id = ?",
                    (task["id"],),
                ).fetchone()
                self.assertEqual(row["status"], "archived")
                self.assertIsNotNone(row["deleted_at"])

    def test_m2_02_delete_archives_pending_task_without_physical_delete(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Archive pending")
                self.assertTrue(delete_task("alice", task["id"]))
                self.assertIsNone(get_task_by_id("alice", task["id"]))
                row = connection.execute(
                    "SELECT status, deleted_at FROM tasks WHERE id = ?",
                    (task["id"],),
                ).fetchone()
                self.assertEqual(row["status"], "archived")
                self.assertIsNotNone(row["deleted_at"])
                self.assertEqual(get_all_tasks("alice"), [])
                self.assertEqual(search_tasks("alice", "Archive pending"), [])

    def test_m2_04_time_fields_are_stored_in_utc_across_year_boundary(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task(
                    "alice",
                    "New year task",
                    planned_start_at="2024-12-31T23:30:00Z",
                    due_at="2025-01-01T00:30:00Z",
                )

                raw = connection.execute(
                    "SELECT planned_start_at, due_at FROM tasks WHERE id = ?",
                    (task["id"],),
                ).fetchone()
                self.assertEqual(raw["planned_start_at"], "2024-12-31T23:30:00Z")
                self.assertEqual(raw["due_at"], "2025-01-01T00:30:00Z")
                self.assertEqual(
                    task["planned_start_at"],
                    convert_utc_to_local("2024-12-31T23:30:00Z"),
                )
                self.assertEqual(
                    task["due_at"],
                    convert_utc_to_local("2025-01-01T00:30:00Z"),
                )

    def test_m2_04_uses_user_timezone_for_naive_input_and_display(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                self.assertEqual(set_user_timezone("alice", "America/New_York"), "America/New_York")
                task = add_task(
                    "alice",
                    "Timezone task",
                    planned_start_at="2025-01-01T00:30:00",
                )
                raw = connection.execute(
                    "SELECT planned_start_at FROM tasks WHERE id = ?",
                    (task["id"],),
                ).fetchone()

                self.assertEqual(raw["planned_start_at"], "2025-01-01T05:30:00Z")
                self.assertEqual(task["planned_start_at"], "2025-01-01 00:30:00 EST")
                with self.assertRaises(InvalidTimestampError):
                    update_task("alice", task["id"], due_at="not-a-timestamp")

    def test_m2_04_rejects_invalid_time_parameters_without_writing(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                with self.assertRaises(InvalidTimestampError):
                    add_task(
                        "alice",
                        "Invalid planned time",
                        planned_start_at="not-a-timestamp",
                    )
                with self.assertRaises(InvalidTimestampError):
                    add_task(
                        "alice",
                        "Invalid due time",
                        due_at="2025-99-99T25:61:00Z",
                    )

                self.assertEqual(get_all_tasks("alice"), [])

    def test_m2_04_explicit_null_time_fields_are_valid(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task(
                    "alice",
                    "Explicitly untimed",
                    planned_start_at=None,
                    due_at=None,
                )

                self.assertIsNone(task["planned_start_at"])
                self.assertIsNone(task["due_at"])

    def test_m2_04_default_timezone_and_dst_boundaries_are_deterministic(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                self.assertEqual(get_user_timezone("alice"), "Asia/Shanghai")
                set_user_timezone("alice", "America/New_York")

                self.assertEqual(
                    convert_utc_to_local("2025-03-09T06:59:59Z", "America/New_York"),
                    "2025-03-09 01:59:59 EST",
                )
                self.assertEqual(
                    convert_utc_to_local("2025-03-09T07:00:00Z", "America/New_York"),
                    "2025-03-09 03:00:00 EDT",
                )
                self.assertEqual(
                    convert_utc_to_local("2025-11-02T05:59:59Z", "America/New_York"),
                    "2025-11-02 01:59:59 EDT",
                )
                self.assertEqual(
                    convert_utc_to_local("2025-11-02T06:00:00Z", "America/New_York"),
                    "2025-11-02 01:00:00 EST",
                )

    def test_m2_05_completion_creates_one_occurrence_and_is_idempotent(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Repeatable completion")
                first = complete_task(
                    "alice",
                    task["id"],
                    occurred_at="2025-01-01T23:30:00Z",
                    notes="Completed during the evening.",
                    idempotency_key="completion-1",
                )
                second = complete_task(
                    "alice",
                    task["id"],
                    occurred_at="2025-01-02T23:30:00Z",
                    idempotency_key="completion-1",
                )

                self.assertEqual(first["id"], second["id"])
                self.assertEqual(first["occurred_at"], second["occurred_at"])
                self.assertEqual(first["notes"], "Completed during the evening.")
                self.assertIsNotNone(first["updated_at"])
                self.assertEqual(len(get_task_occurrences("alice", task["id"])), 1)
                self.assertEqual(get_task_by_id("alice", task["id"])["status"], "completed")

    def test_m2_05_normal_completion_commits_task_and_occurrence_together(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Normal completion")
                occurrence = complete_task(
                    "alice",
                    task["id"],
                    occurred_at="2025-02-01T10:00:00Z",
                    idempotency_key="normal-completion-1",
                )

                stored_task = get_task_by_id("alice", task["id"])
                stored_occurrences = get_task_occurrences("alice", task["id"])
                self.assertEqual(stored_task["status"], "completed")
                self.assertEqual(len(stored_occurrences), 1)
                self.assertEqual(stored_occurrences[0]["id"], occurrence["id"])
                self.assertEqual(stored_occurrences[0]["status"], "completed")
                self.assertEqual(stored_occurrences[0]["task_id"], task["id"])

    def test_m2_05_automatic_occurrence_time_uses_user_timezone_instant(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                set_user_timezone("alice", "Asia/Shanghai")
                task = add_task("alice", "Local now")
                before = datetime.now(timezone.utc)
                occurrence = record_task_occurrence("alice", task["id"])
                after = datetime.now(timezone.utc)

                stored = connection.execute(
                    "SELECT occurred_at FROM task_occurrences WHERE id = ?",
                    (occurrence["id"],),
                ).fetchone()[0]
                stored_at = datetime.fromisoformat(stored.replace("Z", "+00:00"))
                self.assertLessEqual(before, stored_at)
                self.assertLessEqual(stored_at, after)
                self.assertTrue(occurrence["occurred_at"].endswith("CST"))

    def test_m2_05_occurrence_and_task_status_are_committed_together(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Atomic execution")
                with self.assertRaises(InvalidTimestampError):
                    record_task_occurrence(
                        "alice",
                        task["id"],
                        status="completed",
                        started_at="2025-01-02T00:00:00Z",
                        completed_at="2025-01-01T00:00:00Z",
                        idempotency_key="invalid-order",
                    )

                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_occurrences"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(get_task_by_id("alice", task["id"])["status"], "pending")

    def test_m2_05_archived_or_cancelled_task_cannot_be_completed(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                cancelled = add_task("alice", "Cancelled task")
                update_task("alice", cancelled["id"], status="cancelled")
                with self.assertRaises(InvalidTaskStatusError):
                    complete_task("alice", cancelled["id"], idempotency_key="cancelled-1")

                archived = add_task("alice", "Archived task")
                self.assertTrue(delete_task("alice", archived["id"]))
                with self.assertRaises(TaskNotFoundError):
                    complete_task("alice", archived["id"], idempotency_key="archived-1")
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM task_occurrences").fetchone()[0],
                    0,
                )

    def test_m2_05_history_uses_user_timezone_date_boundaries(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                set_user_timezone("alice", "Asia/Shanghai")
                task = add_task("alice", "History task")
                record_task_occurrence(
                    "alice",
                    task["id"],
                    occurred_at="2025-01-01T15:59:59Z",
                    idempotency_key="before-midnight",
                )
                record_task_occurrence(
                    "alice",
                    task["id"],
                    occurred_at="2025-01-01T16:00:00Z",
                    idempotency_key="after-midnight",
                )

                day = get_task_occurrences(
                    "alice",
                    task["id"],
                    start_at="2025-01-02T00:00:00",
                    end_at="2025-01-03T00:00:00",
                )
                self.assertEqual(len(day), 1)
                self.assertEqual(day[0]["idempotency_key"], "after-midnight")

    def test_m2_06_harness_tools_use_trusted_user_context_and_occurrence_service(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                context = ToolCallContext(user_id="alice", request_id="m2-06")
                created = dispatcher.dispatch(
                    "todo_add_task",
                    {"title": "Harness task"},
                    context,
                )
                self.assertTrue(created.ok)
                task_id = created.data["data"]["id"]
                completed = dispatcher.dispatch(
                    "todo_complete_task",
                    {"task_id": task_id, "idempotency_key": "harness-complete-1"},
                    context,
                )
                history = dispatcher.dispatch(
                    "todo_list_occurrences",
                    {"task_id": task_id},
                    context,
                )

                self.assertTrue(completed.ok)
                self.assertTrue(history.ok)
                self.assertEqual(len(history.data["data"]), 1)
                self.assertEqual(
                    dispatcher.dispatch(
                        "todo_list_occurrences",
                        {"task_id": task_id},
                        ToolCallContext(user_id="bob", request_id="m2-06-bob"),
                    ).data["data"],
                    [],
                )

    def test_m2_06_harness_tools_enforce_cross_user_writes_and_pagination(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                dispatcher = ToolDispatcher(
                    build_todo_tool_registry(),
                    authorization=PermissionPolicy(
                        confirmation_callback=lambda context, call, spec: True
                    ),
                )
                alice = ToolCallContext(user_id="alice", request_id="m2-06-alice")
                bob = ToolCallContext(user_id="bob", request_id="m2-06-bob")
                created_ids = []
                for title in ("Alpha one", "Alpha two", "Alpha three"):
                    result = dispatcher.dispatch("todo_add_task", {"title": title}, alice)
                    created_ids.append(result.data["data"]["id"])

                page = dispatcher.dispatch(
                    "todo_list_tasks",
                    {"limit": 2, "offset": 1},
                    alice,
                )
                search = dispatcher.dispatch(
                    "todo_search_tasks",
                    {"query": "Alpha", "limit": 2, "offset": 1},
                    alice,
                )
                self.assertTrue(page.ok)
                self.assertTrue(search.ok)
                self.assertEqual(len(page.data["data"]), 2)
                self.assertEqual(len(search.data["data"]), 2)

                update_result = dispatcher.dispatch(
                    "todo_update_task",
                    {"task_id": created_ids[0], "title": "Hijacked"},
                    bob,
                )
                complete_result = dispatcher.dispatch(
                    "todo_complete_task",
                    {"task_id": created_ids[0], "idempotency_key": "bob-complete"},
                    bob,
                )
                delete_result = dispatcher.dispatch(
                    "todo_delete_task",
                    {"task_id": created_ids[0]},
                    bob,
                )
                self.assertFalse(update_result.ok)
                self.assertFalse(complete_result.ok)
                self.assertFalse(delete_result.ok)
                self.assertEqual(complete_result.error.code, "TASK_NOT_FOUND")
                self.assertEqual(update_result.error.code, "TASK_NOT_FOUND")
                self.assertEqual(delete_result.error.code, "TASK_NOT_FOUND")
                self.assertEqual(get_task_by_id("alice", created_ids[0])["title"], "Alpha one")

    def test_m2_06_confirmation_callback_allows_archive(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Confirmed archive")
                dispatcher = ToolDispatcher(
                    build_todo_tool_registry(),
                    authorization=PermissionPolicy(
                        confirmation_callback=lambda context, call, spec: True
                    ),
                )
                result = dispatcher.dispatch(
                    "todo_delete_task",
                    {"task_id": task["id"]},
                    ToolCallContext(user_id="alice", request_id="m2-06-confirmed"),
                )
                self.assertTrue(result.ok)
                self.assertTrue(result.data["data"]["deleted"])
                self.assertIsNone(get_task_by_id("alice", task["id"]))

    def test_m2_06_delete_tool_requires_confirmation(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                task = add_task("alice", "Protected archive")
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                result = dispatcher.dispatch(
                    "todo_delete_task",
                    {"task_id": task["id"]},
                    ToolCallContext(user_id="alice", request_id="m2-06-delete"),
                )
                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, "CONFIRMATION_REQUIRED")
                self.assertIsNotNone(get_task_by_id("alice", task["id"]))

    def test_m2_02_and_m2_03_validate_database_and_search_boundaries(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            connection.execute("INSERT INTO users(external_key) VALUES (?)", ("alice",))
            user_id = connection.execute(
                "SELECT id FROM users WHERE external_key = 'alice'"
            ).fetchone()[0]

            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO tasks(user_id, title) VALUES (?, ?)",
                    (user_id, ""),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO tasks(user_id, title, status) VALUES (?, ?, ?)",
                    (user_id, "Invalid status", "unknown"),
                )

            with patch("database.db.get_connection", return_value=ConnectionProxy(connection)):
                add_task("alice", "Planning", "Prepare release")
                add_task("alice", "Unrelated")
                self.assertEqual(len(search_tasks("alice", "Plan")), 1)
                self.assertEqual(len(search_tasks("alice", "release")), 1)
                self.assertEqual(search_tasks("alice", "does-not-exist"), [])
                add_task("alice", "literal % marker")
                add_task("alice", "literal _ marker")
                self.assertEqual(len(search_tasks("alice", "%")), 1)
                self.assertEqual(len(search_tasks("alice", "_")), 1)
                with self.assertRaises(ValueError):
                    search_tasks("alice", "   ")
                with self.assertRaises(ValueError):
                    add_task("alice", "x" * 201)

                nullable = add_task("alice", "Nullable times")
                self.assertIsNone(nullable["planned_start_at"])
                self.assertIsNone(nullable["due_at"])
                changed = update_task("alice", nullable["id"], description="Still valid")
                self.assertIsNone(changed["planned_start_at"])
                self.assertIsNone(changed["due_at"])


if __name__ == "__main__":
    unittest.main()
