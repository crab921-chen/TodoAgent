"""Versioned SQLite migrations for the Todo business database."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Callable


class MigrationError(RuntimeError):
    """Raised when a migration cannot be applied or was changed in place."""


Migration = Callable[[sqlite3.Connection], None]


def utc_now() -> str:
    """Return a UTC timestamp in the database's canonical text format."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _legacy_timestamp(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if "T" in text or text.endswith("Z"):
        return text
    return text.replace(" ", "T", 1) + ".000Z"


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _migration_001_initial(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            external_key TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'disabled', 'deleted')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            deleted_at TEXT
        );

        CREATE TABLE IF NOT EXISTS user_profiles (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            default_currency TEXT NOT NULL DEFAULT 'CNY'
                CHECK (length(default_currency) = 3),
            notification_enabled INTEGER NOT NULL DEFAULT 1
                CHECK (notification_enabled IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );
        """
    )

    legacy_table = "tasks_legacy_migration"
    if _table_exists(connection, "tasks"):
        columns = _column_names(connection, "tasks")
        new_columns = {
            "planned_start_at",
            "due_at",
            "deleted_at",
        }
        if not new_columns.issubset(columns):
            connection.execute(f'ALTER TABLE "tasks" RENAME TO "{legacy_table}"')

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
            description TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'in_progress', 'completed', 'cancelled', 'archived')),
            planned_start_at TEXT,
            due_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            deleted_at TEXT,
            UNIQUE (id, user_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_user_status_updated
            ON tasks(user_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_user_planned_start
            ON tasks(user_id, planned_start_at);
        CREATE INDEX IF NOT EXISTS idx_tasks_user_deleted
            ON tasks(user_id, deleted_at);
        """
    )

    if _table_exists(connection, legacy_table):
        connection.execute(
            f"""
            INSERT OR IGNORE INTO users(external_key, status, created_at, updated_at)
            SELECT DISTINCT TRIM(user_id), 'active',
                   COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                   COALESCE(updated_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            FROM "{legacy_table}"
            WHERE user_id IS NOT NULL AND TRIM(user_id) <> ''
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO user_profiles(user_id)
            SELECT id FROM users
            WHERE external_key IN (
                SELECT DISTINCT TRIM(user_id) FROM tasks_legacy_migration
                WHERE user_id IS NOT NULL AND TRIM(user_id) <> ''
            )
            """
        )
        connection.execute(
            f"""
            INSERT INTO tasks(
                id, user_id, title, description, status,
                created_at, updated_at
            )
            SELECT legacy.id,
                   users.id,
                   TRIM(legacy.title),
                   legacy.description,
                   CASE legacy.status
                       WHEN 'pending' THEN 'pending'
                       WHEN 'in_progress' THEN 'in_progress'
                       WHEN 'completed' THEN 'completed'
                       WHEN 'cancelled' THEN 'cancelled'
                       WHEN 'archived' THEN 'archived'
                       ELSE 'pending'
                   END,
                   COALESCE(
                       CASE WHEN INSTR(legacy.created_at, 'T') > 0
                            THEN legacy.created_at
                            ELSE REPLACE(legacy.created_at, ' ', 'T') || '.000Z'
                       END,
                       strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                   ),
                   COALESCE(
                       CASE WHEN INSTR(legacy.updated_at, 'T') > 0
                            THEN legacy.updated_at
                            ELSE REPLACE(legacy.updated_at, ' ', 'T') || '.000Z'
                       END,
                       strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                   )
            FROM "{legacy_table}" AS legacy
            JOIN users ON users.external_key = TRIM(legacy.user_id)
            """
        )
        connection.execute(f'DROP TABLE "{legacy_table}"')


def _migration_002_occurrences(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS task_occurrences (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed'
                CHECK (status IN ('started', 'completed', 'cancelled')),
            planned_at TEXT,
            started_at TEXT,
            completed_at TEXT,
            occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            source TEXT NOT NULL DEFAULT 'agent',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            idempotency_key TEXT,
            UNIQUE (user_id, idempotency_key),
            FOREIGN KEY (task_id, user_id) REFERENCES tasks(id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_occurrences_task_occurred
            ON task_occurrences(task_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_occurrences_user_occurred
            ON task_occurrences(user_id, occurred_at);
        """
    )


def _migration_003_occurrence_metadata(connection: sqlite3.Connection) -> None:
    """Add mutable execution metadata without rewriting occurrence history."""

    columns = _column_names(connection, "task_occurrences")
    if "notes" not in columns:
        connection.execute("ALTER TABLE task_occurrences ADD COLUMN notes TEXT")
    if "updated_at" not in columns:
        connection.execute("ALTER TABLE task_occurrences ADD COLUMN updated_at TEXT")
        connection.execute(
            """
            UPDATE task_occurrences
            SET updated_at = COALESCE(created_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            WHERE updated_at IS NULL
            """
        )
    if "deleted_at" not in columns:
        connection.execute("ALTER TABLE task_occurrences ADD COLUMN deleted_at TEXT")
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_occurrences_user_status_occurred
            ON task_occurrences(user_id, status, occurred_at)
        """
    )


def _migration_004_reminders_goal(connection: sqlite3.Connection) -> None:
    """Add durable reminders, recurrence rules, and period goals."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER,
            content TEXT NOT NULL CHECK (length(content) BETWEEN 1 AND 500),
            trigger_at TEXT NOT NULL,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            status TEXT NOT NULL DEFAULT 'scheduled'
                CHECK (status IN (
                    'scheduled', 'processing', 'sent', 'failed',
                    'cancelled', 'expired'
                )),
            attempt_count INTEGER NOT NULL DEFAULT 0
                CHECK (attempt_count >= 0),
            last_error TEXT,
            next_retry_at TEXT,
            sent_at TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id, user_id) REFERENCES tasks(id, user_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_reminders_user_status_trigger
            ON reminders(user_id, status, trigger_at);
        CREATE INDEX IF NOT EXISTS idx_reminders_status_retry
            ON reminders(status, next_retry_at);

        CREATE TABLE IF NOT EXISTS recurrence_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            frequency TEXT NOT NULL
                CHECK (frequency IN ('daily', 'weekly', 'monthly', 'custom')),
            interval_value INTEGER NOT NULL DEFAULT 1
                CHECK (interval_value > 0),
            start_date TEXT NOT NULL,
            end_date TEXT,
            timezone TEXT NOT NULL DEFAULT 'UTC',
            active INTEGER NOT NULL DEFAULT 1
                CHECK (active IN (0, 1)),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            CHECK (end_date IS NULL OR end_date >= start_date),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id, user_id) REFERENCES tasks(id, user_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_recurrence_user_active
            ON recurrence_rules(user_id, active, start_date);
        CREATE INDEX IF NOT EXISTS idx_recurrence_task
            ON recurrence_rules(task_id, active);

        CREATE TABLE IF NOT EXISTS goal_progress (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            period_start TEXT NOT NULL,
            period_end TEXT NOT NULL,
            target_count INTEGER NOT NULL CHECK (target_count > 0),
            completed_count INTEGER NOT NULL DEFAULT 0
                CHECK (completed_count >= 0),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'completed', 'expired')),
            completed_at TEXT,
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (user_id, task_id, period_start),
            CHECK (period_end > period_start),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id, user_id) REFERENCES tasks(id, user_id)
                ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_goal_progress_user_period
            ON goal_progress(user_id, period_start, period_end);
        CREATE INDEX IF NOT EXISTS idx_goal_progress_task_period
            ON goal_progress(task_id, period_start, period_end);
        """
    )


def _migration_005_finance_audit(connection: sqlite3.Connection) -> None:
    """Add transactions, explicit task links, and runtime metadata."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL DEFAULT 'expense'
                CHECK (type IN ('expense', 'income')),
            amount_minor INTEGER NOT NULL
                CHECK (typeof(amount_minor) = 'integer' AND amount_minor > 0),
            currency TEXT NOT NULL DEFAULT 'CNY'
                CHECK (
                    length(currency) = 3
                    AND currency = upper(currency)
                    AND currency GLOB '[A-Z][A-Z][A-Z]'
                ),
            category TEXT NOT NULL CHECK (length(category) BETWEEN 1 AND 100),
            note TEXT,
            occurred_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            created_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            deleted_at TEXT,
            UNIQUE (id, user_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_transactions_user_occurred
            ON transactions(user_id, occurred_at);
        CREATE INDEX IF NOT EXISTS idx_transactions_user_type_category
            ON transactions(user_id, type, category);

        CREATE TABLE IF NOT EXISTS task_transaction_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            transaction_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            basis TEXT NOT NULL DEFAULT 'user_confirmed'
                CHECK (basis IN ('explicit', 'user_confirmed')),
            confirmed_at TEXT,
            created_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (user_id, task_id, transaction_id),
            FOREIGN KEY (task_id, user_id) REFERENCES tasks(id, user_id)
                ON DELETE CASCADE,
            FOREIGN KEY (transaction_id, user_id)
                REFERENCES transactions(id, user_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_task_transaction_links_user
            ON task_transaction_links(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_task_transaction_links_task
            ON task_transaction_links(task_id, created_at);

        CREATE TABLE IF NOT EXISTS agent_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_key TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'archived')),
            last_request_id TEXT,
            created_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            expires_at TEXT,
            UNIQUE (user_id, session_key),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_agent_sessions_user_updated
            ON agent_sessions(user_id, updated_at);

        CREATE TABLE IF NOT EXISTS tool_audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id INTEGER,
            session_key TEXT,
            request_id TEXT NOT NULL,
            tool_call_id TEXT,
            tool_name TEXT NOT NULL,
            permission_level TEXT NOT NULL DEFAULT 'read'
                CHECK (permission_level IN ('read', 'write', 'high_risk')),
            args_summary TEXT,
            input_digest TEXT,
            success INTEGER NOT NULL DEFAULT 0 CHECK (success IN (0, 1)),
            status TEXT NOT NULL DEFAULT 'failed'
                CHECK (status IN ('success', 'failed', 'denied', 'timeout')),
            duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
            error_code TEXT,
            created_at TEXT NOT NULL
                DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tool_audit_request
            ON tool_audit_logs(request_id);
        CREATE INDEX IF NOT EXISTS idx_tool_audit_user_created
            ON tool_audit_logs(user_id, created_at);
        """
    )


def _migration_006_conversations(connection: sqlite3.Connection) -> None:
    """Add durable conversation messages and tool-turn metadata for M6-M7."""

    columns = _column_names(connection, "agent_sessions")
    if "title" not in columns:
        connection.execute("ALTER TABLE agent_sessions ADD COLUMN title TEXT")
    if "archived_at" not in columns:
        connection.execute("ALTER TABLE agent_sessions ADD COLUMN archived_at TEXT")
    connection.execute(
        """
        UPDATE agent_sessions
        SET title = COALESCE(title, session_key)
        WHERE title IS NULL
        """
    )
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            sequence_no INTEGER NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool', 'system')),
            message_type TEXT NOT NULL DEFAULT 'message',
            content TEXT,
            content_digest TEXT NOT NULL,
            client_message_id TEXT,
            tool_call_id TEXT,
            metadata_json TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (session_id, sequence_no),
            UNIQUE (user_id, client_message_id),
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_messages_session_sequence
            ON conversation_messages(session_id, sequence_no);
        CREATE INDEX IF NOT EXISTS idx_conversation_messages_user_created
            ON conversation_messages(user_id, created_at);

        CREATE TABLE IF NOT EXISTS conversation_tool_turns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id INTEGER,
            session_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            request_id TEXT NOT NULL,
            tool_call_id TEXT,
            tool_name TEXT NOT NULL,
            arguments_digest TEXT,
            result_summary TEXT,
            status TEXT NOT NULL CHECK (status IN ('started', 'success', 'failed', 'denied', 'timeout')),
            duration_ms INTEGER CHECK (duration_ms IS NULL OR duration_ms >= 0),
            error_code TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            FOREIGN KEY (message_id) REFERENCES conversation_messages(id) ON DELETE SET NULL,
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_conversation_tool_turns_session_created
            ON conversation_tool_turns(session_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_conversation_tool_turns_request
            ON conversation_tool_turns(request_id);
        """
    )


def _migration_007_m8_task_reminder_metadata(connection: sqlite3.Connection) -> None:
    """Add source metadata required by the unified M8 task/reminder flows."""

    task_columns = _column_names(connection, "tasks")
    if "source" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")
    if "request_id" not in task_columns:
        connection.execute("ALTER TABLE tasks ADD COLUMN request_id TEXT")

    reminder_columns = _column_names(connection, "reminders")
    if "source" not in reminder_columns:
        connection.execute("ALTER TABLE reminders ADD COLUMN source TEXT NOT NULL DEFAULT 'manual'")

    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_tasks_user_request
            ON tasks(user_id, request_id);
        CREATE INDEX IF NOT EXISTS idx_reminders_user_source
            ON reminders(user_id, source);
        """
    )


def _migration_008_m9_plans(connection: sqlite3.Connection) -> None:
    """Add durable plan drafts, plans, phases, task links, and dependencies."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS plan_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id INTEGER,
            request_id TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            warnings_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'draft'
                CHECK (status IN ('draft', 'applied', 'expired', 'rejected')),
            confirmation_digest TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            applied_plan_id INTEGER,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (user_id, request_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
            goal TEXT NOT NULL CHECK (length(goal) BETWEEN 1 AND 10000),
            status TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('draft', 'confirmed', 'active', 'completed', 'archived')),
            source_session_id INTEGER,
            version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
            batch_id TEXT NOT NULL,
            milestones_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (user_id, batch_id),
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (source_session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS plan_phases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            name TEXT NOT NULL CHECK (length(name) BETWEEN 1 AND 200),
            sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
            start_date TEXT,
            end_date TEXT,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'active', 'completed', 'archived')),
            UNIQUE (plan_id, sequence_no),
            FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE,
            CHECK (end_date IS NULL OR start_date IS NULL OR end_date >= start_date)
        );

        CREATE TABLE IF NOT EXISTS plan_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            phase_id INTEGER NOT NULL,
            task_id INTEGER NOT NULL,
            relation_type TEXT NOT NULL DEFAULT 'primary',
            sequence_no INTEGER NOT NULL CHECK (sequence_no > 0),
            UNIQUE (plan_id, task_id),
            FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE,
            FOREIGN KEY (phase_id) REFERENCES plan_phases(id) ON DELETE CASCADE,
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS plan_dependencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            predecessor_task_id INTEGER NOT NULL,
            successor_task_id INTEGER NOT NULL,
            dependency_type TEXT NOT NULL DEFAULT 'finish_to_start',
            UNIQUE (plan_id, predecessor_task_id, successor_task_id),
            CHECK (predecessor_task_id <> successor_task_id),
            FOREIGN KEY (plan_id) REFERENCES plans(id) ON DELETE CASCADE,
            FOREIGN KEY (predecessor_task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (successor_task_id) REFERENCES tasks(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_plan_drafts_user_status
            ON plan_drafts(user_id, status, expires_at);
        CREATE INDEX IF NOT EXISTS idx_plans_user_status
            ON plans(user_id, status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_plan_tasks_task
            ON plan_tasks(task_id);
        CREATE INDEX IF NOT EXISTS idx_plan_dependencies_plan
            ON plan_dependencies(plan_id);
        """
    )


def _migration_009_m10_charts(connection: sqlite3.Connection) -> None:
    """Add user-scoped statistics query definitions and chart artifacts."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS chart_queries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            session_id INTEGER,
            query_json TEXT NOT NULL,
            range_start TEXT NOT NULL,
            range_end TEXT NOT NULL,
            timezone TEXT NOT NULL,
            query_digest TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            expires_at TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS chart_artifacts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            query_id INTEGER NOT NULL,
            chart_type TEXT NOT NULL
                CHECK (chart_type IN ('line', 'bar', 'pie', 'table')),
            spec_json TEXT NOT NULL,
            data_digest TEXT NOT NULL,
            spec_digest TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            UNIQUE (query_id, spec_digest),
            FOREIGN KEY (query_id) REFERENCES chart_queries(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_chart_queries_user_created
            ON chart_queries(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_chart_queries_user_digest
            ON chart_queries(user_id, query_digest);
        CREATE INDEX IF NOT EXISTS idx_chart_artifacts_query_expires
            ON chart_artifacts(query_id, expires_at);
        """
    )


def _migration_010_m11_exports(connection: sqlite3.Connection) -> None:
    """Add durable user export records without storing raw file paths."""

    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS backup_exports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            export_id TEXT NOT NULL UNIQUE,
            user_id INTEGER NOT NULL,
            scope TEXT NOT NULL
                CHECK (scope IN ('all', 'tasks', 'plans', 'finance', 'conversations')),
            format TEXT NOT NULL CHECK (format IN ('json')),
            manifest_json TEXT NOT NULL,
            path_digest TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'completed'
                CHECK (status IN ('pending', 'completed', 'failed', 'expired')),
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
            expires_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_backup_exports_user_created
            ON backup_exports(user_id, created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_backup_exports_user_status
            ON backup_exports(user_id, status, expires_at);
        """
    )


@dataclass(frozen=True)
class MigrationDefinition:
    version: str
    source: str
    apply: Migration

    @property
    def checksum(self) -> str:
        return sha256(self.source.encode("utf-8")).hexdigest()


MIGRATIONS = (
    MigrationDefinition(
        version="001_initial",
        source="users user_profiles tasks legacy-task-migration",
        apply=_migration_001_initial,
    ),
    MigrationDefinition(
        version="002_occurrences",
        source="task_occurrences task-execution-records",
        apply=_migration_002_occurrences,
    ),
    MigrationDefinition(
        version="003_occurrence_metadata",
        source="task_occurrences notes updated_at deleted_at occurrence-index",
        apply=_migration_003_occurrence_metadata,
    ),
    MigrationDefinition(
        version="004_reminders_goal",
        source="reminders recurrence_rules goal_progress scheduler-retry-goals",
        apply=_migration_004_reminders_goal,
    ),
    MigrationDefinition(
        version="005_finance_audit",
        source="transactions task_transaction_links agent_sessions tool_audit_logs",
        apply=_migration_005_finance_audit,
    ),
    MigrationDefinition(
        version="006_conversations",
        source="agent_sessions conversation_messages conversation_tool_turns",
        apply=_migration_006_conversations,
    ),
    MigrationDefinition(
        version="007_m8_task_reminder_metadata",
        source="tasks.source tasks.request_id reminders.source",
        apply=_migration_007_m8_task_reminder_metadata,
    ),
    MigrationDefinition(
        version="008_m9_plans",
        source="plan_drafts plans plan_phases plan_tasks plan_dependencies",
        apply=_migration_008_m9_plans,
    ),
    MigrationDefinition(
        version="009_m10_charts",
        source="chart_queries chart_artifacts statistics-chart-cache",
        apply=_migration_009_m10_charts,
    ),
    MigrationDefinition(
        version="010_m11_exports",
        source="backup_exports user-scoped-json-export",
        apply=_migration_010_m11_exports,
    ),
)


def open_connection(database_path: str | Path) -> sqlite3.Connection:
    """Open a configured SQLite connection with foreign keys enabled."""

    connection = sqlite3.connect(
        str(database_path),
        timeout=30,
        check_same_thread=False,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def apply_migrations(
    database_path: str | Path | sqlite3.Connection,
) -> tuple[str, ...]:
    """Apply all pending migrations atomically and return applied versions."""

    owns_connection = not isinstance(database_path, sqlite3.Connection)
    if owns_connection:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = open_connection(path)
    else:
        connection = database_path
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
    applied: list[str] = []
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL,
                checksum TEXT NOT NULL
            )
            """
        )
        connection.commit()

        for migration in MIGRATIONS:
            row = connection.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?",
                (migration.version,),
            ).fetchone()
            if row is not None:
                if row["checksum"] != migration.checksum:
                    raise MigrationError(
                        f"Migration checksum mismatch: {migration.version}"
                    )
                continue

            try:
                connection.execute("BEGIN")
                migration.apply(connection)
                connection.execute(
                    """
                    INSERT INTO schema_migrations(version, applied_at, checksum)
                    VALUES (?, ?, ?)
                    """,
                    (migration.version, utc_now(), migration.checksum),
                )
                connection.commit()
                applied.append(migration.version)
            except Exception as error:
                connection.rollback()
                raise MigrationError(
                    f"Migration failed: {migration.version}"
                ) from error
        return tuple(applied)
    finally:
        if owns_connection:
            connection.close()
