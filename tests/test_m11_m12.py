"""M11 export and M12 safety regression tests."""

from __future__ import annotations

import json
import os
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import database.db as db
import database.maintenance as maintenance
from database.db import add_task, create_transaction
from database.exports import create_user_export, list_user_exports
from database.migrations import apply_migrations
from harness.security import PermissionPolicy
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry
from ops.runtime import ConfigurationError, load_config


class M11M12Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = Path.cwd() / f".m11-m12-test-{uuid4().hex}"
        self.temp_dir.mkdir()
        self.database_path = self.temp_dir / "todo.db"
        apply_migrations(self.database_path)
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = self.database_path

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.original_database_path
        for path in sorted(self.temp_dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                path.rmdir()
        self.temp_dir.rmdir()

    def test_m11_migration_creates_export_table(self) -> None:
        connection = sqlite3.connect(self.database_path)
        try:
            table = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'backup_exports'"
            ).fetchone()
            version = connection.execute(
                "SELECT version FROM schema_migrations ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(table[0], "backup_exports")
        self.assertEqual(version[0], "010_m11_exports")

    def test_export_is_user_scoped_redacted_and_idempotent(self) -> None:
        add_task("alice", "Alice task")
        add_task("bob", "Bob task")
        create_transaction(
            "alice",
            "expense",
            100,
            "test",
            note="token " + "gsk_" + "sensitivevalue123456789",
        )
        create_transaction("bob", "expense", 200, "test", note="Bob private")

        first = create_user_export(
            "alice",
            scope="finance",
            export_format="json",
            request_id="export-request-1",
        )
        repeated = create_user_export(
            "alice",
            scope="finance",
            export_format="json",
            request_id="export-request-1",
        )
        payload = json.loads(
            (self.database_path.parent / ".todo-exports" / f"{first['export_id']}.json")
            .read_text(encoding="utf-8")
        )

        self.assertFalse(first["idempotent"])
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(first["export_id"], repeated["export_id"])
        self.assertEqual(len(payload["tables"]["transactions"]), 1)
        self.assertEqual(payload["tables"]["transactions"][0]["user_id"], 1)
        self.assertNotIn("Bob private", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("gsk_" + "sensitivevalue123456789", json.dumps(payload))
        self.assertIn("<redacted-authorization>", json.dumps(payload))

        self.assertEqual(len(list_user_exports("alice")), 1)
        self.assertEqual(list_user_exports("bob"), [])

        all_export = create_user_export(
            "alice",
            scope="all",
            export_format="json",
            request_id="export-request-all",
        )
        all_payload = json.loads(
            (self.database_path.parent / ".todo-exports" / f"{all_export['export_id']}.json")
            .read_text(encoding="utf-8")
        )
        self.assertIn("plans", all_payload["tables"])
        self.assertIn("chart_artifacts", all_payload["tables"])
        self.assertNotIn("Bob private", json.dumps(all_payload, ensure_ascii=False))

    def test_backup_export_tool_requires_high_risk_confirmation(self) -> None:
        dispatcher = ToolDispatcher(build_todo_tool_registry())
        context = ToolCallContext(user_id="alice", request_id="export-tool")
        denied = dispatcher.dispatch("backup_export", {"scope": "tasks"}, context)
        self.assertFalse(denied.ok)
        self.assertEqual(denied.error.code, "CONFIRMATION_REQUIRED")

        confirmed_dispatcher = ToolDispatcher(
            build_todo_tool_registry(),
            authorization=PermissionPolicy(confirmation_callback=lambda *_: True),
        )
        confirmed = confirmed_dispatcher.dispatch(
            "backup_export", {"scope": "tasks"}, context
        )
        self.assertTrue(confirmed.ok)
        self.assertEqual(confirmed.data["data"]["scope"], "tasks")

    def test_restore_rejects_backup_directory_and_preserves_target_on_validation_failure(self) -> None:
        source = self.database_path
        backup = Path(self.temp_dir.name) / "backup.db"
        maintenance.backup_database(source, backup)

        with self.assertRaises(maintenance.MaintenanceError):
            maintenance.restore_bundle(
                self.temp_dir.name,
                database_path=Path(self.temp_dir.name) / "nested" / "todo.db",
            )

        target = Path(self.temp_dir.name) / "target.db"
        target.write_bytes(b"original target")
        source_report = maintenance._integrity(backup)
        original_integrity = maintenance._integrity

        def fake_integrity(path: Path) -> dict[str, object]:
            report = original_integrity(path)
            if path.name.endswith(".restore"):
                report = {**report, "row_counts": {}}
            return report

        with patch.object(maintenance, "_integrity", side_effect=fake_integrity):
            with self.assertRaises(maintenance.MaintenanceError):
                maintenance.restore_database(backup, target)
        self.assertEqual(target.read_bytes(), b"original target")
        self.assertTrue(source_report["ok"])

    def test_explicit_model_name_is_validated_without_falling_back_to_dotenv(self) -> None:
        with patch.dict(os.environ, {"GROQ_MODEL": " "}, clear=False):
            with self.assertRaises(ConfigurationError):
                load_config()


if __name__ == "__main__":
    unittest.main()
