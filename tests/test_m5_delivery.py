import json
import shutil
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier, Event
from time import sleep
from unittest.mock import patch
from uuid import uuid4

from database.db import (
    add_task,
    complete_task,
    get_all_tasks,
    get_task_occurrences,
)
from database.m3 import claim_due_reminders, create_reminder
from database.m4 import create_transaction, get_transaction, list_tool_audits
from database.maintenance import (
    MaintenanceError,
    backup_database,
    backup_bundle,
    migrate_with_backup,
    reconcile_database,
    restore_bundle,
    restore_database,
)
from database.migrations import apply_migrations, open_connection
from harness.agent_loop import AgentLoop
from harness.messages import ModelResponse, ToolCall
from harness.model_runtime import FallbackModelInvoker
from harness.security import PermissionPolicy
from harness.tool_protocol import CancellationToken, ToolCallContext
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


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def complete(self, messages, tools, cancellation_token):
        self.calls += 1
        return self.responses.pop(0)


class RaisingModel:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def complete(self, messages, tools, cancellation_token):
        self.calls += 1
        raise self.error


class RecordingModel:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def complete(self, messages, tools, cancellation_token):
        self.calls += 1
        return self.response


class FailingFallback:
    def complete(self, messages, tools, cancellation_token):
        raise RuntimeError("fallback unavailable")


class M5DeliveryTests(unittest.TestCase):
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

    def temporary_directory(self):
        @contextmanager
        def directory_context():
            directory = Path.cwd() / "database" / f"m5-test-{uuid4().hex}"
            directory.mkdir()
            try:
                yield directory
            finally:
                shutil.rmtree(directory, ignore_errors=True)

        return directory_context()

    def test_m5_01_unit_validation_rejects_untrusted_input_without_writing(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                result = dispatcher.dispatch(
                    "todo_add_task",
                    {"title": ""},
                    ToolCallContext(user_id="alice", request_id="m5-unit"),
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, "INVALID_ARGUMENTS")
                self.assertEqual(get_all_tasks("alice"), [])

    def test_m5_01_input_boundaries_reject_injection_and_wrong_types_without_writes(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                context = ToolCallContext(user_id="alice", request_id="m5-boundary")
                results = [
                    dispatcher.dispatch(
                        "todo_add_task",
                        {"title": "x" * 201},
                        context,
                    ),
                    dispatcher.dispatch(
                        "transaction_create",
                        {"type": "expense", "amount_minor": "900", "category": "x"},
                        context,
                    ),
                    dispatcher.dispatch(
                        "todo_search_tasks",
                        {"query": "' OR 1=1 --", "limit": 0},
                        context,
                    ),
                ]

                self.assertTrue(all(not result.ok for result in results))
                self.assertTrue(all(result.error.code == "INVALID_ARGUMENTS" for result in results))
                self.assertEqual(get_all_tasks("alice"), [])

    def test_m5_01_integration_persists_business_data_and_audit_metadata(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                context = ToolCallContext(user_id="alice", request_id="m5-integration")
                task = dispatcher.dispatch(
                    "todo_add_task", {"title": "Integration task"}, context
                )
                transaction = dispatcher.dispatch(
                    "transaction_create",
                    {
                        "type": "expense",
                        "amount_minor": 1200,
                        "category": "testing",
                    },
                    context,
                )

                self.assertTrue(task.ok)
                self.assertTrue(transaction.ok)
                self.assertEqual(len(get_all_tasks("alice")), 1)
                self.assertEqual(
                    get_transaction("alice", transaction.data["data"]["id"])[
                        "amount_minor"
                    ],
                    1200,
                )
                audits = list_tool_audits("alice", request_id="m5-integration")
                self.assertEqual(len(audits), 2)
                self.assertTrue(all(item["input_digest"] for item in audits))
                self.assertTrue(all("1200" not in (item["args_summary"] or "") for item in audits))

    def test_m5_01_end_to_end_agent_loop_executes_tool_and_returns_response(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            model = ScriptedModel(
                [
                    ModelResponse(
                        tool_calls=[
                            ToolCall(
                                id="call-1",
                                name="todo_add_task",
                                arguments={"title": "E2E task"},
                            )
                        ]
                    ),
                    ModelResponse(content="Task created."),
                ]
            )
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                result = AgentLoop(model, build_todo_tool_registry()).run(
                    "Create an E2E task.",
                    ToolCallContext(user_id="alice", request_id="m5-e2e"),
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.final_response, "Task created.")
                self.assertEqual(result.tool_call_count, 1)
                self.assertEqual(get_all_tasks("alice")[0]["title"], "E2E task")

    def test_m5_01_security_negative_cases_are_user_scoped_and_non_mutating(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                task = add_task("alice", "Private task")
                transaction = create_transaction("alice", "expense", 800, "private")
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                bob = ToolCallContext(user_id="bob", request_id="m5-security")

                task_result = dispatcher.dispatch(
                    "todo_update_task",
                    {"task_id": task["id"], "title": "Hijacked"},
                    bob,
                )
                transaction_result = dispatcher.dispatch(
                    "transaction_get",
                    {"transaction_id": transaction["id"]},
                    bob,
                )
                delete_result = dispatcher.dispatch(
                    "todo_delete_task", {"task_id": task["id"]}, bob
                )

                self.assertFalse(task_result.ok)
                self.assertEqual(task_result.error.code, "TASK_NOT_FOUND")
                self.assertFalse(transaction_result.ok)
                self.assertEqual(transaction_result.error.code, "TRANSACTION_NOT_FOUND")
                self.assertFalse(delete_result.ok)
                self.assertEqual(delete_result.error.code, "CONFIRMATION_REQUIRED")
                self.assertEqual(get_all_tasks("alice")[0]["title"], "Private task")
                self.assertIsNotNone(get_transaction("alice", transaction["id"]))

    def test_m5_01_audit_logs_are_user_scoped_and_denied_writes_are_audited(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                task = add_task("alice", "Audit isolation")
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                bob = ToolCallContext(user_id="bob", request_id="m5-audit-bob")
                result = dispatcher.dispatch(
                    "todo_delete_task", {"task_id": task["id"]}, bob
                )

                self.assertFalse(result.ok)
                self.assertEqual(result.error.code, "CONFIRMATION_REQUIRED")
                self.assertEqual(list_tool_audits("bob", request_id="m5-audit-bob")[0]["status"], "denied")
                self.assertEqual(list_tool_audits("alice", request_id="m5-audit-bob"), [])

    def test_m5_01_confirmed_and_rejected_high_risk_writes_have_distinct_outcomes(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                task = add_task("alice", "Confirmation branches")
                denied = ToolDispatcher(build_todo_tool_registry()).dispatch(
                    "todo_delete_task",
                    {"task_id": task["id"]},
                    ToolCallContext(user_id="alice", request_id="m5-denied"),
                )
                confirmed = ToolDispatcher(
                    build_todo_tool_registry(),
                    authorization=PermissionPolicy(
                        confirmation_callback=lambda context, call, spec: True
                    ),
                ).dispatch(
                    "todo_delete_task",
                    {"task_id": task["id"]},
                    ToolCallContext(user_id="alice", request_id="m5-confirmed"),
                )

                self.assertEqual(denied.error.code, "CONFIRMATION_REQUIRED")
                self.assertTrue(confirmed.ok)
                self.assertEqual(get_all_tasks("alice"), [])

    def test_m5_01_all_high_risk_finance_and_link_writes_require_confirmation(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                task = add_task("alice", "Protected link")
                transaction = create_transaction("alice", "expense", 100, "protected")
                context = ToolCallContext(user_id="alice", request_id="m5-high-risk")
                dispatcher = ToolDispatcher(build_todo_tool_registry())
                voided = dispatcher.dispatch(
                    "transaction_void",
                    {"transaction_id": transaction["id"]},
                    context,
                )
                link_denied = dispatcher.dispatch(
                    "task_transaction_link",
                    {
                        "task_id": task["id"],
                        "transaction_id": transaction["id"],
                    },
                    context,
                )
                confirmed_dispatcher = ToolDispatcher(
                    build_todo_tool_registry(),
                    authorization=PermissionPolicy(
                        confirmation_callback=lambda context, call, spec: True
                    ),
                )
                link = confirmed_dispatcher.dispatch(
                    "task_transaction_link",
                    {
                        "task_id": task["id"],
                        "transaction_id": transaction["id"],
                    },
                    context,
                )
                link_deleted = dispatcher.dispatch(
                    "task_transaction_link_delete",
                    {"link_id": link.data["data"]["id"]},
                    context,
                )
                confirmed_deleted = confirmed_dispatcher.dispatch(
                    "task_transaction_link_delete",
                    {"link_id": link.data["data"]["id"]},
                    context,
                )

                self.assertEqual(voided.error.code, "CONFIRMATION_REQUIRED")
                self.assertEqual(link_denied.error.code, "CONFIRMATION_REQUIRED")
                self.assertTrue(link.ok)
                self.assertEqual(link_deleted.error.code, "CONFIRMATION_REQUIRED")
                self.assertTrue(confirmed_deleted.ok)

    def test_m5_01_tool_timeout_and_cancellation_return_structured_errors(self):
        from pydantic import BaseModel
        from harness.tool_protocol import ToolContract, ToolSpec
        from harness.tool_registry import ToolRegistry

        class Input(BaseModel):
            value: str

        started = Event()
        release = Event()

        def slow(arguments, context):
            started.set()
            release.wait()
            return {"value": arguments.value}

        contract = ToolContract(
            ToolSpec(
                name="slow_m5",
                description="Slow M5 test tool.",
                input_model=Input,
                timeout_seconds=0.01,
            ),
            slow,
        )
        try:
            timeout_result = ToolDispatcher(ToolRegistry([contract])).dispatch(
                "slow_m5",
                {"value": "timeout"},
                ToolCallContext(user_id="alice", request_id="m5-timeout"),
            )
        finally:
            release.set()

        self.assertEqual(timeout_result.error.code, "TOOL_TIMEOUT")

    def test_m5_02_concurrent_claims_deliver_a_due_reminder_once(self):
        with self.temporary_directory() as directory:
            database_path = Path(directory) / "todo.db"
            apply_migrations(database_path)
            with patch(
                "database.db.get_connection",
                side_effect=lambda: open_connection(database_path),
            ):
                reminder = create_reminder(
                    "alice", "Concurrent delivery", "2025-01-01T10:00:00Z", timezone_name="UTC"
                )

            barrier = Barrier(2)

            def claim():
                barrier.wait()
                return claim_due_reminders("alice", now="2025-01-01T10:00:00Z")

            with patch(
                "database.db.get_connection",
                side_effect=lambda: open_connection(database_path),
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    results = list(executor.map(lambda _: claim(), range(2)))

            claimed_ids = [item["id"] for result in results for item in result]
            self.assertEqual(claimed_ids, [reminder["id"]])

    def test_m5_02_sqlite_lock_waits_for_writer_before_claiming(self):
        with self.temporary_directory() as directory:
            database_path = directory / "locked.db"
            apply_migrations(database_path)
            with patch(
                "database.db.get_connection",
                side_effect=lambda: open_connection(database_path),
            ):
                reminder = create_reminder(
                    "alice", "Lock wait", "2025-01-01T10:00:00Z", timezone_name="UTC"
                )

            writer = open_connection(database_path)
            writer.execute("BEGIN IMMEDIATE")
            started = Event()

            def claim():
                started.set()
                return claim_due_reminders("alice", now="2025-01-01T10:00:00Z")

            try:
                with patch(
                    "database.db.get_connection",
                    side_effect=lambda: open_connection(database_path),
                ):
                    with ThreadPoolExecutor(max_workers=1) as executor:
                        future = executor.submit(claim)
                        self.assertTrue(started.wait(1))
                        sleep(0.1)
                        writer.commit()
                        claimed = future.result(timeout=3)
            finally:
                writer.close()

            self.assertEqual([item["id"] for item in claimed], [reminder["id"]])

    def test_m5_02_idempotent_execution_and_structured_exception_are_recoverable(self):
        with self.temporary_database() as connection:
            apply_migrations(connection)
            with patch(
                "database.db.get_connection",
                return_value=ConnectionProxy(connection),
            ):
                task = add_task("alice", "Retry-safe task")
                first = complete_task(
                    "alice", task["id"], idempotency_key="retry-1", occurred_at="2025-01-01T10:00:00Z"
                )
                second = complete_task(
                    "alice", task["id"], idempotency_key="retry-1", occurred_at="2025-01-01T11:00:00Z"
                )
                invalid = ToolDispatcher(build_todo_tool_registry()).dispatch(
                    "transaction_create",
                    {"type": "expense", "amount_minor": -1, "category": "bad"},
                    ToolCallContext(user_id="alice", request_id="m5-exception"),
                )

                self.assertEqual(first["id"], second["id"])
                self.assertEqual(len(get_task_occurrences("alice", task["id"])), 1)
                self.assertFalse(invalid.ok)
                self.assertEqual(invalid.error.code, "INVALID_ARGUMENTS")

    def test_m5_02_fallback_model_is_used_only_for_primary_failure(self):
        fallback_response = ModelResponse(content="fallback")
        fallback = RecordingModel(fallback_response)
        primary_error = RuntimeError("primary unavailable")
        seen = []
        invoker = FallbackModelInvoker(
            RaisingModel(primary_error), fallback, on_fallback=seen.append
        )

        response = invoker.complete([], [], CancellationToken())

        self.assertEqual(response.content, "fallback")
        self.assertEqual(fallback.calls, 1)
        self.assertEqual(seen, [primary_error])

        token = CancellationToken()
        token.cancel()
        cancelled_fallback = RecordingModel(ModelResponse(content="must not run"))
        with self.assertRaises(RuntimeError):
            FallbackModelInvoker(
                RaisingModel(RuntimeError("cancelled")), cancelled_fallback
            ).complete([], [], token)
        self.assertEqual(cancelled_fallback.calls, 0)

        with self.assertRaises(RuntimeError):
            FallbackModelInvoker(
                RaisingModel(RuntimeError("primary")), FailingFallback()
            ).complete([], [], CancellationToken())

    def test_m5_03_backup_restore_and_reconciliation_preserve_database_state(self):
        with self.temporary_directory() as directory:
            root = Path(directory)
            database_path = root / "todo.db"
            backup_path = root / "todo.backup.db"
            apply_migrations(database_path)
            with patch(
                "database.db.get_connection",
                side_effect=lambda: open_connection(database_path),
            ):
                task = add_task("alice", "Backup me")

            backup = backup_database(database_path, backup_path)
            self.assertTrue(backup["backup"]["ok"])
            connection = open_connection(database_path)
            try:
                connection.execute("DELETE FROM tasks WHERE id = ?", (task["id"],))
                connection.commit()
            finally:
                connection.close()
            restore = restore_database(backup_path, database_path)

            self.assertTrue(restore["target"]["ok"])
            self.assertEqual(reconcile_database(database_path)["schema_versions"][-1], "010_m11_exports")
            with patch(
                "database.db.get_connection",
                side_effect=lambda: open_connection(database_path),
            ):
                self.assertEqual(get_all_tasks("alice")[0]["id"], task["id"])

    def test_m5_03_restore_bundle_rejects_missing_or_tampered_backup(self):
        with self.temporary_directory() as directory:
            database_path = directory / "todo.db"
            backup_directory = directory / "bundle"
            apply_migrations(database_path)
            backup_bundle(
                database_path=database_path,
                backup_directory=backup_directory,
            )
            (backup_directory / "todo.db").unlink()
            with self.assertRaisesRegex(Exception, "backup file is missing"):
                restore_bundle(backup_directory, database_path=directory / "restored.db")

            backup_bundle(
                database_path=database_path,
                backup_directory=backup_directory,
            )
            with (backup_directory / "todo.db").open("ab") as stream:
                stream.write(b"tampered")
            with self.assertRaisesRegex(Exception, "manifest does not match"):
                restore_bundle(backup_directory, database_path=directory / "restored.db")

            (backup_directory / "manifest.json").write_text("{broken", encoding="utf-8")
            with self.assertRaisesRegex(MaintenanceError, "manifest is not valid JSON"):
                restore_bundle(backup_directory, database_path=directory / "restored.db")

    def test_m5_03_reconciliation_reports_schema_errors(self):
        with self.temporary_directory() as directory:
            database_path = directory / "schema-error.db"
            apply_migrations(database_path)
            connection = open_connection(database_path)
            try:
                connection.execute("DROP TABLE tool_audit_logs")
                connection.commit()
            finally:
                connection.close()

            report = reconcile_database(database_path)

            self.assertFalse(report["ok"])
            self.assertIn("missing table: tool_audit_logs", report["schema_errors"])

    def test_m5_03_bundle_covers_business_and_checkpoint_databases(self):
        with self.temporary_directory() as directory:
            root = Path(directory)
            database_path = root / "todo.db"
            checkpoints_path = root / "checkpoints.db"
            backup_directory = root / "bundle"
            apply_migrations(database_path)
            apply_migrations(checkpoints_path)
            checkpoint_connection = open_connection(checkpoints_path)
            try:
                checkpoint_connection.execute(
                    "CREATE TABLE checkpoint_marker (id INTEGER)"
                )
                checkpoint_connection.commit()
            finally:
                checkpoint_connection.close()

            manifest = backup_bundle(
                database_path=database_path,
                checkpoints_path=checkpoints_path,
                backup_directory=backup_directory,
            )
            self.assertTrue((backup_directory / "manifest.json").is_file())
            self.assertIsNotNone(manifest["checkpoints"])
            self.assertTrue(json.loads((backup_directory / "manifest.json").read_text())["database"])

            restored_database = root / "restored.db"
            restored_checkpoints = root / "restored-checkpoints.db"
            restored = restore_bundle(
                backup_directory,
                database_path=restored_database,
                checkpoints_path=restored_checkpoints,
            )
            self.assertTrue(restored["database"]["target"]["ok"])
            self.assertTrue(restored["checkpoints"]["target"]["ok"])
            connection = open_connection(restored_checkpoints)
            try:
                self.assertIsNotNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'checkpoint_marker'"
                    ).fetchone()
                )
            finally:
                connection.close()

    def test_m5_03_migration_failure_restores_original_database(self):
        with self.temporary_directory() as directory:
            database_path = Path(directory) / "todo.db"
            apply_migrations(database_path)
            with patch(
                "database.db.get_connection",
                side_effect=lambda: open_connection(database_path),
            ):
                task = add_task("alice", "Keep after rollback")

            def failing_migration(path):
                connection = open_connection(path)
                try:
                    connection.execute("CREATE TABLE partial_migration (id INTEGER)")
                    connection.commit()
                finally:
                    connection.close()
                raise RuntimeError("forced migration failure")

            with patch("database.maintenance.apply_migrations", failing_migration):
                with self.assertRaises(RuntimeError):
                    migrate_with_backup(database_path)

            connection = open_connection(database_path)
            try:
                self.assertIsNone(
                    connection.execute(
                        "SELECT 1 FROM sqlite_master WHERE name = 'partial_migration'"
                    ).fetchone()
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM tasks WHERE id = ?", (task["id"],)).fetchone()[0],
                    1,
                )
            finally:
                connection.close()

    def test_m5_03_reconciliation_reports_foreign_key_inconsistency(self):
        with self.temporary_directory() as directory:
            database_path = directory / "inconsistent.db"
            apply_migrations(database_path)
            connection = open_connection(database_path)
            try:
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "INSERT INTO tasks(user_id, title) VALUES (?, ?)",
                    (999999, "orphan"),
                )
                connection.commit()
            finally:
                connection.close()

            report = reconcile_database(database_path)

            self.assertFalse(report["ok"])
            self.assertEqual(report["integrity_check"], "ok")
            self.assertTrue(report["foreign_key_errors"])

    def test_m5_03_fresh_migration_with_backup_reconciles_cleanly(self):
        with self.temporary_directory() as directory:
            database_path = Path(directory) / "fresh.db"
            result = migrate_with_backup(database_path)

            self.assertEqual(result["applied_migrations"][-1], "010_m11_exports")
            self.assertTrue(result["verification"]["ok"])
            self.assertEqual(result["verification"]["foreign_key_errors"], [])


if __name__ == "__main__":
    unittest.main()
