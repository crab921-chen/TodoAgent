"""M9 plan draft, confirmation, DAG, and atomic application tests."""

from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from uuid import uuid4

import database.db as db
from database.migrations import apply_migrations
from database.db import complete_task
from database.plans import (
    PlanDraftError,
    PlanNotFoundError,
    apply_plan,
    archive_plan,
    create_plan_draft,
    get_plan,
    list_plans,
)
from harness.security import PermissionPolicy
from harness.tool_protocol import ToolCallContext
from harness.tool_registry import ToolDispatcher
from harness.todo_tools import build_todo_tool_registry


class M9PlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.database_path = Path.cwd() / f".m9-test-{uuid4().hex}.db"
        apply_migrations(self.database_path)
        self.original_database_path = db.DATABASE_PATH
        db.DATABASE_PATH = self.database_path

    def tearDown(self) -> None:
        db.DATABASE_PATH = self.original_database_path
        self.database_path.unlink(missing_ok=True)

    def test_m9_draft_is_preview_only_and_apply_creates_all_entities(self) -> None:
        draft = create_plan_draft(
            "alice",
            intent="Prepare a product launch",
            constraints={
                "title": "Product launch",
                "phases": [
                    {"name": "Build", "tasks": [
                        {"key": "build", "title": "Build release"},
                        {"key": "test", "title": "Test release"},
                    ]},
                    {"name": "Launch", "tasks": [{"key": "announce", "title": "Announce release"}]},
                ],
                "dependencies": [
                    {"predecessor": "build", "successor": "test"},
                    {"predecessor": "test", "successor": "announce"},
                ],
            },
            request_id="plan-request-1",
        )
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
        finally:
            connection.close()

        applied = apply_plan("alice", draft["draft_id"], draft["confirmation_token"])
        self.assertFalse(applied["idempotent"])
        plan = get_plan("alice", applied["plan_id"])
        self.assertEqual(plan["title"], "Product launch")
        self.assertEqual(len(plan["phases"]), 2)
        self.assertEqual(len(plan["dependencies"]), 2)
        self.assertEqual(len(applied["created_task_ids"]), 3)

    def test_m9_invalid_token_and_cycle_never_write_plan(self) -> None:
        with self.assertRaises(PlanDraftError):
            create_plan_draft(
                "alice",
                intent="Cycle",
                constraints={
                    "phases": [{"name": "Phase", "tasks": [
                        {"key": "a", "title": "A"}, {"key": "b", "title": "B"}
                    ]}],
                    "dependencies": [
                        {"predecessor": "a", "successor": "b"},
                        {"predecessor": "b", "successor": "a"},
                    ],
                },
                request_id="cycle",
            )
        valid = create_plan_draft("alice", intent="Valid", request_id="valid")
        with self.assertRaises(PlanDraftError):
            apply_plan("alice", valid["draft_id"], "wrong-token")
        self.assertEqual(list_plans("alice"), [])

    def test_m9_apply_is_idempotent_and_user_scoped(self) -> None:
        draft = create_plan_draft("alice", intent="Idempotent", request_id="same-batch")
        applied = apply_plan("alice", draft["draft_id"], draft["confirmation_token"])
        repeated = apply_plan("alice", draft["draft_id"], "unused-after-apply")
        self.assertTrue(repeated["idempotent"])
        self.assertEqual(repeated["plan_id"], applied["plan_id"])
        with self.assertRaises(PlanNotFoundError):
            apply_plan("bob", draft["draft_id"], draft["confirmation_token"])

    def test_m9_tool_contract_requires_confirmation_for_apply(self) -> None:
        dispatcher = ToolDispatcher(build_todo_tool_registry())
        context = ToolCallContext(user_id="alice", request_id="tool-draft")
        draft_result = dispatcher.dispatch("plan_draft", {"intent": "Tool plan"}, context)
        self.assertTrue(draft_result.ok)
        apply_result = dispatcher.dispatch(
            "plan_apply",
            {
                "draft_id": draft_result.data["data"]["draft_id"],
                "confirmation_token": draft_result.data["data"]["confirmation_token"],
            },
            context,
        )
        self.assertFalse(apply_result.ok)
        self.assertEqual(apply_result.error.code, "CONFIRMATION_REQUIRED")

        confirmed = ToolDispatcher(
            build_todo_tool_registry(),
            authorization=PermissionPolicy(confirmation_callback=lambda *_: True),
        )
        result = confirmed.dispatch(
            "plan_apply",
            {
                "draft_id": draft_result.data["data"]["draft_id"],
                "confirmation_token": draft_result.data["data"]["confirmation_token"],
            },
            context,
        )
        self.assertTrue(result.ok)

    def test_m9_archive_stops_plan_recurrence_without_deleting_task_history(self) -> None:
        draft = create_plan_draft(
            "alice",
            intent="Recurring plan",
            constraints={
                "phases": [{"name": "Daily", "tasks": [{
                    "key": "daily", "title": "Daily work",
                    "recurrence": {"frequency": "daily", "start_date": "2025-01-01"},
                }]}],
            },
            request_id="recurring-plan",
        )
        applied = apply_plan("alice", draft["draft_id"], draft["confirmation_token"])
        self.assertTrue(archive_plan("alice", applied["plan_id"]))
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(connection.execute("SELECT status FROM plans WHERE id = ?", (applied["plan_id"],)).fetchone()[0], "archived")
            self.assertEqual(connection.execute("SELECT active FROM recurrence_rules").fetchone()[0], 0)
        finally:
            connection.close()

    def test_m9_task_execution_rolls_plan_and_phase_status_forward(self) -> None:
        draft = create_plan_draft(
            "alice",
            intent="Status propagation",
            constraints={
                "phases": [
                    {"name": "First", "tasks": [{"key": "one", "title": "One"}]},
                    {"name": "Second", "tasks": [{"key": "two", "title": "Two"}]},
                ]
            },
            request_id="status-propagation",
        )
        applied = apply_plan("alice", draft["draft_id"], draft["confirmation_token"])
        plan = get_plan("alice", applied["plan_id"])
        first_task = plan["phases"][0]["tasks"][0]["task_id"]
        second_task = plan["phases"][1]["tasks"][0]["task_id"]

        complete_task("alice", first_task, idempotency_key="status-one")
        updated = get_plan("alice", applied["plan_id"])
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["phases"][0]["status"], "completed")
        self.assertEqual(updated["phases"][1]["status"], "pending")

        complete_task("alice", second_task, idempotency_key="status-two")
        completed = get_plan("alice", applied["plan_id"])
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(
            [phase["status"] for phase in completed["phases"]],
            ["completed", "completed"],
        )

    def test_m9_versioning_archives_previous_version_and_preserves_history(self) -> None:
        first = create_plan_draft("alice", intent="Version one", request_id="version-one")
        first_applied = apply_plan("alice", first["draft_id"], first["confirmation_token"])
        first_plan = get_plan("alice", first_applied["plan_id"])
        first_task = first_plan["phases"][0]["tasks"][0]["task_id"]
        complete_task("alice", first_task, idempotency_key="version-history")

        second = create_plan_draft(
            "alice",
            intent="Version two",
            constraints={"base_plan_id": first_applied["plan_id"]},
            request_id="version-two",
        )
        second_applied = apply_plan("alice", second["draft_id"], second["confirmation_token"])
        old = get_plan("alice", first_applied["plan_id"])
        current = get_plan("alice", second_applied["plan_id"])
        self.assertEqual(old["status"], "archived")
        self.assertEqual(old["phases"][0]["status"], "archived")
        self.assertEqual(old["version"], 1)
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["status"], "active")
        self.assertEqual(len(old["phases"][0]["tasks"]), 1)
        self.assertEqual(old["phases"][0]["tasks"][0]["status"], "completed")

    def test_m9_invalid_batch_rolls_back_every_business_row(self) -> None:
        draft = create_plan_draft(
            "alice",
            intent="Rollback plan",
            constraints={
                "phases": [{
                    "name": "Phase",
                    "tasks": [{
                        "title": "Task",
                        "existing_task_id": 999999,
                    }],
                }]
            },
            request_id="rollback-plan",
        )
        with self.assertRaises(PlanDraftError):
            apply_plan("alice", draft["draft_id"], draft["confirmation_token"])
        connection = sqlite3.connect(self.database_path)
        try:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM plans").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM plan_phases").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM tasks").fetchone()[0], 0)
            self.assertEqual(
                connection.execute("SELECT status FROM plan_drafts WHERE id = ?", (draft["draft_id"],)).fetchone()[0],
                "draft",
            )
        finally:
            connection.close()

    def test_m9_draft_rejects_invalid_dates_and_dependency_types(self) -> None:
        with self.assertRaises(PlanDraftError):
            create_plan_draft(
                "alice",
                intent="Invalid date",
                constraints={
                    "phases": [{
                        "name": "Phase",
                        "tasks": [{
                            "title": "Task",
                            "planned_start_at": "2026-01-02T10:00:00Z",
                            "due_at": "2026-01-01T10:00:00Z",
                        }],
                    }]
                },
                request_id="invalid-date",
            )
        with self.assertRaises(PlanDraftError):
            create_plan_draft(
                "alice",
                intent="Invalid dependency",
                constraints={
                    "phases": [{"name": "Phase", "tasks": [{"key": "a", "title": "A"}]}],
                    "dependencies": [{
                        "predecessor": "a",
                        "successor": "a",
                        "dependency_type": "not-allowed",
                    }],
                },
                request_id="invalid-dependency",
            )


if __name__ == "__main__":
    unittest.main()
