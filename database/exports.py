"""M11 user-scoped JSON export service."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from database import db
from database.maintenance import EXPORT_FORMATS, EXPORT_SCOPES
from database.migrations import open_connection


class ExportError(ValueError):
    """An export request is invalid or cannot be completed safely."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


_TABLES = {
    "tasks": "SELECT * FROM tasks WHERE user_id = ? ORDER BY id",
    "task_occurrences": "SELECT * FROM task_occurrences WHERE user_id = ? ORDER BY id",
    "reminders": "SELECT * FROM reminders WHERE user_id = ? ORDER BY id",
    "recurrence_rules": "SELECT * FROM recurrence_rules WHERE user_id = ? ORDER BY id",
    "goal_progress": "SELECT * FROM goal_progress WHERE user_id = ? ORDER BY id",
    "transactions": "SELECT * FROM transactions WHERE user_id = ? ORDER BY id",
    "task_transaction_links": "SELECT * FROM task_transaction_links WHERE user_id = ? ORDER BY id",
    "agent_sessions": "SELECT * FROM agent_sessions WHERE user_id = ? ORDER BY id",
    "conversation_messages": "SELECT * FROM conversation_messages WHERE user_id = ? ORDER BY id",
    "conversation_tool_turns": "SELECT * FROM conversation_tool_turns WHERE user_id = ? ORDER BY id",
    "tool_audit_logs": "SELECT * FROM tool_audit_logs WHERE user_id = ? ORDER BY id",
    "plan_drafts": "SELECT * FROM plan_drafts WHERE user_id = ? ORDER BY id",
    "plans": "SELECT * FROM plans WHERE user_id = ? ORDER BY id",
    "plan_phases": "SELECT plan_phases.* FROM plan_phases JOIN plans ON plans.id = plan_phases.plan_id WHERE plans.user_id = ? ORDER BY plan_phases.id",
    "plan_tasks": "SELECT plan_tasks.* FROM plan_tasks JOIN plans ON plans.id = plan_tasks.plan_id WHERE plans.user_id = ? ORDER BY plan_tasks.id",
    "plan_dependencies": "SELECT plan_dependencies.* FROM plan_dependencies JOIN plans ON plans.id = plan_dependencies.plan_id WHERE plans.user_id = ? ORDER BY plan_dependencies.id",
    "chart_queries": "SELECT * FROM chart_queries WHERE user_id = ? ORDER BY id",
    "chart_artifacts": "SELECT chart_artifacts.* FROM chart_artifacts JOIN chart_queries ON chart_queries.id = chart_artifacts.query_id WHERE chart_queries.user_id = ? ORDER BY chart_artifacts.id",
}

_SCOPES = {
    "tasks": ("tasks", "task_occurrences", "reminders", "recurrence_rules", "goal_progress"),
    "plans": ("plan_drafts", "plans", "plan_phases", "plan_tasks", "plan_dependencies"),
    "finance": ("transactions", "task_transaction_links"),
    "conversations": ("agent_sessions", "conversation_messages", "conversation_tool_turns"),
}


def _rows(connection, table: str, user_pk: int) -> list[dict[str, Any]]:
    rows = [dict(row) for row in connection.execute(_TABLES[table], (user_pk,)).fetchall()]
    for row in rows:
        for key, value in list(row.items()):
            if key.endswith("_json") and isinstance(value, str):
                try:
                    row[key] = json.loads(value)
                except json.JSONDecodeError:
                    pass
    return rows


def create_user_export(
    user_id: str,
    *,
    scope: str,
    export_format: str,
    request_id: str,
    database_path: str | Path | None = None,
    ttl_seconds: int = 86400,
) -> dict[str, Any]:
    scope = str(scope).strip().lower()
    export_format = str(export_format).strip().lower()
    request_id = str(request_id).strip()
    if scope not in EXPORT_SCOPES:
        raise ExportError("scope is not supported")
    if export_format not in EXPORT_FORMATS:
        raise ExportError("format is not supported")
    if not request_id or len(request_id) > 200:
        raise ExportError("request_id is invalid")
    if ttl_seconds < 60 or ttl_seconds > 604800:
        raise ExportError("ttl_seconds must be between 60 and 604800")
    root = Path(database_path or db.DATABASE_PATH).resolve().parent / ".todo-exports"
    root.mkdir(parents=True, exist_ok=True)

    connection = open_connection(database_path or db.DATABASE_PATH)
    try:
        user_pk = db._ensure_user(connection, db._validate_user_id(user_id))
        export_id = "export-" + _digest({"user_id": user_id, "request_id": request_id})[:32]
        existing = connection.execute(
            "SELECT * FROM backup_exports WHERE export_id = ? AND user_id = ?",
            (export_id, user_pk),
        ).fetchone()
        if existing is not None and existing["status"] == "completed":
            manifest = json.loads(existing["manifest_json"])
            return {
                "export_id": existing["export_id"],
                "scope": existing["scope"],
                "format": existing["format"],
                "status": existing["status"],
                "manifest": manifest,
                "path_digest": existing["path_digest"],
                "expires_at": existing["expires_at"],
                "idempotent": True,
            }

        tables = list(_TABLES) if scope == "all" else list(_SCOPES[scope])
        # Relationship rows have their own user_id in this schema; only known
        # user-scoped tables are selected, so no caller-controlled SQL is used.
        payload = {
            "version": 1,
            "export_id": export_id,
            "user_id": db._validate_user_id(user_id),
            "scope": scope,
            "format": export_format,
            "created_at": _utc_text(_now()),
            "tables": {table: _rows(connection, table, user_pk) for table in tables if table in _TABLES},
        }
        # Import lazily because the ops package exposes Harness symbols that
        # also import this module during package initialization.
        from ops.observability import redact_value

        payload = redact_value(payload)
        file_name = f"{export_id}.json"
        export_path = root / file_name
        temporary = root / f".{file_name}.{uuid4().hex}.tmp"
        serialized = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            temporary.write_bytes(serialized)
            os.replace(temporary, export_path)
        finally:
            temporary.unlink(missing_ok=True)
        expires_at = _utc_text(_now() + timedelta(seconds=ttl_seconds))
        manifest = {
            "version": payload["version"],
            "export_id": export_id,
            "scope": scope,
            "format": export_format,
            "created_at": payload["created_at"],
            "tables": {table: len(rows) for table, rows in payload["tables"].items()},
            "size_bytes": len(serialized),
        }
        path_digest = hashlib.sha256(str(export_path).encode("utf-8")).hexdigest()
        connection.execute(
            """
            INSERT INTO backup_exports(
                export_id, user_id, scope, format, manifest_json,
                path_digest, status, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'completed', ?)
            ON CONFLICT(export_id) DO UPDATE SET
                manifest_json = excluded.manifest_json,
                path_digest = excluded.path_digest,
                status = excluded.status,
                expires_at = excluded.expires_at
            """,
            (export_id, user_pk, scope, export_format, _canonical(manifest), path_digest, expires_at),
        )
        connection.commit()
        return {
            "export_id": export_id,
            "scope": scope,
            "format": export_format,
            "status": "completed",
            "manifest": manifest,
            "path_digest": path_digest,
            "expires_at": expires_at,
            "idempotent": False,
        }
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def list_user_exports(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500:
        raise ExportError("limit must be between 1 and 500")
    connection = db.get_connection()
    try:
        user_pk = connection.execute(
            "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
            (db._validate_user_id(user_id),),
        ).fetchone()
        if user_pk is None:
            return []
        return [
            {**dict(row), "manifest": json.loads(row["manifest_json"])}
            for row in connection.execute(
                "SELECT * FROM backup_exports WHERE user_id = ? ORDER BY id DESC LIMIT ?",
                (int(user_pk["id"]), limit),
            ).fetchall()
        ]
    finally:
        connection.close()


__all__ = ["ExportError", "create_user_export", "list_user_exports"]
