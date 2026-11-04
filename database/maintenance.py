"""M5 backup, restore, migration safety, and database reconciliation helpers."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from database.migrations import MIGRATIONS, apply_migrations, open_connection


class MaintenanceError(RuntimeError):
    """A backup, restore, verification, or migration safety operation failed."""


EXPORT_SCOPES = frozenset({"all", "tasks", "plans", "finance", "conversations"})
EXPORT_FORMATS = frozenset({"json"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _copy_sqlite(source: Path, target: Path) -> None:
    if not source.is_file():
        raise MaintenanceError(f"SQLite source does not exist: {source}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        if temporary.exists():
            temporary.unlink()
        source_connection = sqlite3.connect(str(source), timeout=30)
        target_connection = sqlite3.connect(str(temporary), timeout=30)
        try:
            source_connection.backup(target_connection)
            target_connection.commit()
        finally:
            target_connection.close()
            source_connection.close()
        os.replace(temporary, target)
    except Exception as error:
        if temporary.exists():
            temporary.unlink()
        raise MaintenanceError(f"SQLite backup failed: {source}") from error


def _integrity(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise MaintenanceError(f"SQLite database does not exist: {path}")
    connection = open_connection(path)
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = [
            dict(row)
            for row in connection.execute("PRAGMA foreign_key_check").fetchall()
        ]
        tables = [
            str(row["name"])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            ).fetchall()
        ]
        row_counts: dict[str, int] = {}
        for table in tables:
            if table.startswith("sqlite_"):
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            row_counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            )
        versions = []
        if "schema_migrations" in tables:
            versions = [
                str(row["version"])
                for row in connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall()
            ]
        return {
            "path": str(path),
            "integrity_check": integrity,
            "foreign_key_errors": foreign_keys,
            "tables": tables,
            "row_counts": row_counts,
            "schema_versions": versions,
            "sha256": _sha256(path),
            "size_bytes": path.stat().st_size,
            "ok": integrity == "ok" and not foreign_keys,
        }
    finally:
        connection.close()


def _schema_errors(report: dict[str, Any]) -> list[str]:
    """Detect missing business schema objects and migration records."""

    expected_versions = [migration.version for migration in MIGRATIONS]
    expected_tables = {
        "users",
        "user_profiles",
        "tasks",
        "task_occurrences",
        "reminders",
        "recurrence_rules",
        "goal_progress",
        "transactions",
        "task_transaction_links",
        "agent_sessions",
        "conversation_messages",
        "conversation_tool_turns",
        "tool_audit_logs",
        "plan_drafts",
        "plans",
        "plan_phases",
        "plan_tasks",
        "plan_dependencies",
        "chart_queries",
        "chart_artifacts",
        "backup_exports",
        "schema_migrations",
    }
    tables = set(report["tables"])
    errors = [f"missing table: {table}" for table in sorted(expected_tables - tables)]
    versions = report["schema_versions"]
    if versions != expected_versions:
        errors.append("schema migration versions do not match the current release")
    return errors


def reconcile_database(database_path: str | Path) -> dict[str, Any]:
    """Return an integrity, schema, and row-count reconciliation report."""

    report = _integrity(Path(database_path).resolve())
    report["schema_errors"] = _schema_errors(report)
    report["ok"] = report["ok"] and not report["schema_errors"]
    return report


def backup_database(
    source_path: str | Path,
    backup_path: str | Path,
) -> dict[str, Any]:
    """Create a consistent SQLite backup using SQLite's backup API."""

    source = Path(source_path).resolve()
    backup = Path(backup_path).resolve()
    before = _integrity(source)
    if not before["ok"]:
        raise MaintenanceError("refusing to back up an inconsistent SQLite database")
    _copy_sqlite(source, backup)
    after = _integrity(backup)
    if not after["ok"]:
        raise MaintenanceError("backup failed integrity verification")
    return {
        "created_at": _utc_now(),
        "source": before,
        "backup": after,
    }


def restore_database(
    backup_path: str | Path,
    target_path: str | Path,
) -> dict[str, Any]:
    """Restore one database after verifying the backup before replacement."""

    backup = Path(backup_path).resolve()
    target = Path(target_path).resolve()
    source_report = _integrity(backup)
    if not source_report["ok"]:
        raise MaintenanceError("refusing to restore an inconsistent backup")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.{uuid4().hex}.restore")
    try:
        _copy_sqlite(backup, temporary)
        restored = _integrity(temporary)
        if not restored["ok"]:
            raise MaintenanceError("restored database failed integrity verification")
        if (
            source_report["row_counts"] != restored["row_counts"]
            or source_report["schema_versions"] != restored["schema_versions"]
        ):
            raise MaintenanceError("restored database does not match the backup manifest")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "restored_at": _utc_now(),
        "backup": source_report,
        "target": restored,
    }


def backup_bundle(
    *,
    database_path: str | Path,
    checkpoints_path: str | Path | None = None,
    backup_directory: str | Path,
) -> dict[str, Any]:
    """Back up business and checkpoint databases with a machine-readable manifest."""

    source = Path(database_path).resolve()
    checkpoint = Path(checkpoints_path).resolve() if checkpoints_path else None
    directory = Path(backup_directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    backup_business = directory / "todo.db"
    manifest: dict[str, Any] = {
        "created_at": _utc_now(),
        "database": backup_database(source, backup_business),
        "checkpoints": None,
    }
    if checkpoint is not None and checkpoint.exists():
        manifest["checkpoints"] = backup_database(
            checkpoint,
            directory / "checkpoints.db",
        )
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def restore_bundle(
    backup_directory: str | Path,
    *,
    database_path: str | Path,
    checkpoints_path: str | Path | None = None,
) -> dict[str, Any]:
    """Restore a backup bundle and verify every restored database."""

    directory = Path(backup_directory).resolve()
    database_target = Path(database_path).resolve()
    checkpoint_target = Path(checkpoints_path).resolve() if checkpoints_path else None
    if database_target == directory or database_target.is_relative_to(directory):
        raise MaintenanceError("restore target must not be inside the backup directory")
    if checkpoint_target is not None and (
        checkpoint_target == directory or checkpoint_target.is_relative_to(directory)
    ):
        raise MaintenanceError("checkpoint restore target must not be inside the backup directory")
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        raise MaintenanceError(f"backup manifest does not exist: {manifest_path}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise MaintenanceError("backup manifest is not valid JSON") from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get("database"), dict):
        raise MaintenanceError("backup manifest has no database entry")

    def verify_entry(filename: str, entry: dict[str, Any]) -> None:
        backup_path = directory / filename
        if not backup_path.is_file():
            raise MaintenanceError(f"backup file is missing: {filename}")
        expected = entry.get("backup")
        if not isinstance(expected, dict):
            raise MaintenanceError(f"backup manifest entry is invalid: {filename}")
        actual = _integrity(backup_path)
        for field in ("sha256", "size_bytes", "row_counts", "schema_versions"):
            if actual.get(field) != expected.get(field):
                raise MaintenanceError(f"backup manifest does not match: {filename}")

    verify_entry("todo.db", manifest["database"])
    if manifest.get("checkpoints") is not None:
        if not isinstance(manifest["checkpoints"], dict):
            raise MaintenanceError("checkpoint backup manifest entry is invalid")
        verify_entry("checkpoints.db", manifest["checkpoints"])
    result: dict[str, Any] = {
        "database": restore_database(directory / "todo.db", database_target),
        "checkpoints": None,
    }
    checkpoint_backup = directory / "checkpoints.db"
    if checkpoints_path is not None and checkpoint_backup.exists():
        result["checkpoints"] = restore_database(checkpoint_backup, checkpoint_target)
    return result


def migrate_with_backup(
    database_path: str | Path,
    *,
    checkpoints_path: str | Path | None = None,
    backup_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Back up first, apply migrations, reconcile, and restore on failure."""

    database = Path(database_path).resolve()
    checkpoints = Path(checkpoints_path).resolve() if checkpoints_path else None
    owns_backup_directory = backup_directory is None
    if owns_backup_directory:
        directory = database.parent / (
            f".todo-m5-backup-{os.getpid()}-{uuid4().hex}"
        )
        directory.mkdir(parents=True, exist_ok=False)
    else:
        directory = Path(backup_directory).resolve()
    try:
        if database.exists():
            manifest = backup_bundle(
                database_path=database,
                checkpoints_path=checkpoints,
                backup_directory=directory,
            )
        else:
            manifest = None
        try:
            applied = apply_migrations(database)
            verification = reconcile_database(database)
            if not verification["ok"]:
                raise MaintenanceError("database failed post-migration reconciliation")
            return {
                "applied_migrations": applied,
                "backup": manifest,
                "verification": verification,
            }
        except Exception:
            if manifest is not None:
                restore_bundle(
                    directory,
                    database_path=database,
                    checkpoints_path=checkpoints,
                )
            raise
    finally:
        if owns_backup_directory:
            shutil.rmtree(directory, ignore_errors=True)


__all__ = [
    "MaintenanceError",
    "backup_bundle",
    "backup_database",
    "migrate_with_backup",
    "reconcile_database",
    "restore_bundle",
    "restore_database",
]
