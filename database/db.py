"""User-scoped Todo persistence backed by the M2 schema."""

from __future__ import annotations

import sqlite3
import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from database.migrations import open_connection


load_dotenv(Path(__file__).resolve().parent.parent / ".env")

DATABASE_PATH = Path(
    os.getenv("TODO_DATABASE_PATH", str(Path(__file__).resolve().parent / "todo.db"))
)
LOCAL_TIMEZONE = ZoneInfo(os.getenv("TODO_DEFAULT_TIMEZONE", "Asia/Shanghai"))
DEFAULT_USER_TIMEZONE = LOCAL_TIMEZONE.key
TASK_STATUSES = frozenset(
    {"pending", "in_progress", "completed", "cancelled", "archived"}
)
ALLOWED_STATUS_TRANSITIONS = {
    "pending": frozenset({"in_progress", "completed", "cancelled"}),
    "in_progress": frozenset({"completed", "pending", "cancelled"}),
    "completed": frozenset({"pending", "in_progress"}),
    "cancelled": frozenset({"archived"}),
    "archived": frozenset(),
}


class TaskNotFoundError(LookupError):
    """The requested task is not owned by the current user."""


class InvalidTaskStatusError(ValueError):
    """A task status or state transition is not allowed."""


class InvalidTimestampError(ValueError):
    """A timestamp is missing timezone information or has an invalid value."""


def _validate_user_id(user_id: str) -> str:
    value = str(user_id).strip()
    if not value:
        raise ValueError("user_id must not be blank")
    if len(value) > 200:
        raise ValueError("user_id is too long")
    return value


def _validate_title(title: str) -> str:
    value = str(title).strip()
    if not 1 <= len(value) <= 200:
        raise ValueError("title length must be between 1 and 200 characters")
    return value


def _validate_description(description: str | None) -> str | None:
    if description is None:
        return None
    value = str(description)
    if len(value) > 10000:
        raise ValueError("description is too long")
    return value


def _validate_source(source: str) -> str:
    value = str(source).strip()
    if not 1 <= len(value) <= 100:
        raise ValueError("source must be between 1 and 100 characters")
    return value


def _validate_timezone(timezone_name: str) -> str:
    value = str(timezone_name).strip()
    if not value:
        raise ValueError("timezone must not be blank")
    try:
        ZoneInfo(value)
    except (KeyError, ValueError) as error:
        raise ValueError(f"invalid IANA timezone: {value}") from error
    return value


def _parse_timestamp(
    value: str | datetime,
    *,
    timezone_name: str,
    field_name: str,
) -> datetime:
    """Parse an input timestamp and normalize it to an aware UTC datetime."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        if not text:
            raise InvalidTimestampError(f"{field_name} must not be blank")
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as error:
            raise InvalidTimestampError(
                f"{field_name} must be an ISO-8601 timestamp"
            ) from error
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(_validate_timezone(timezone_name)))
    return parsed.astimezone(timezone.utc)


def _format_utc_timestamp(value: datetime) -> str:
    """Format an aware datetime in the canonical UTC storage format."""

    value = value.astimezone(timezone.utc)
    timespec = "microseconds" if value.microsecond else "seconds"
    return value.isoformat(timespec=timespec).replace("+00:00", "Z")


def _now_utc_timestamp(timezone_name: str = DEFAULT_USER_TIMEZONE) -> str:
    """Return the current instant after resolving it in a user's timezone."""

    local_now = datetime.now(ZoneInfo(_validate_timezone(timezone_name)))
    return _format_utc_timestamp(local_now)


def _normalize_timestamp(
    value: str | datetime | None,
    *,
    timezone_name: str,
    field_name: str,
) -> str | None:
    if value is None:
        return None
    return _format_utc_timestamp(
        _parse_timestamp(value, timezone_name=timezone_name, field_name=field_name)
    )


def convert_utc_to_local(
    timestamp: str | None,
    timezone_name: str = DEFAULT_USER_TIMEZONE,
) -> str | None:
    """Convert a stored UTC timestamp to a user's local display format."""

    if timestamp is None:
        return None
    text = str(timestamp).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(ZoneInfo(_validate_timezone(timezone_name))).strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )


def serialize_task(
    task: sqlite3.Row | None,
    timezone_name: str = DEFAULT_USER_TIMEZONE,
    *,
    include_metadata: bool = False,
) -> Optional[dict[str, Any]]:
    """Convert a database task row into the existing tool response shape."""

    if task is None:
        return None
    data = dict(task)
    if "user_external_key" in data:
        data["user_id"] = data.pop("user_external_key")
    data.pop("user_timezone", None)
    if not include_metadata:
        data.pop("source", None)
        data.pop("request_id", None)
    for field in ("created_at", "updated_at", "planned_start_at", "due_at"):
        if field in data:
            data[field] = convert_utc_to_local(data[field], timezone_name)
    return data


def get_connection() -> sqlite3.Connection:
    """Create a foreign-key-enabled connection to the configured database."""

    return open_connection(DATABASE_PATH)


def _ensure_user(connection: sqlite3.Connection, external_key: str) -> int:
    """Resolve the trusted application user key to the internal user ID."""

    connection.execute(
        """
        INSERT INTO users(external_key)
        VALUES (?)
        ON CONFLICT(external_key) DO NOTHING
        """,
        (external_key,),
    )
    row = connection.execute(
        "SELECT id, status FROM users WHERE external_key = ?",
        (external_key,),
    ).fetchone()
    if row is None or row["status"] != "active":
        raise PermissionError("user is not active")
    connection.execute(
        """
        INSERT OR IGNORE INTO user_profiles(user_id, timezone)
        VALUES (?, ?)
        """,
        (row["id"], DEFAULT_USER_TIMEZONE),
    )
    return int(row["id"])


def _user_profile(connection: sqlite3.Connection, user_pk: int) -> sqlite3.Row:
    row = connection.execute(
        "SELECT timezone FROM user_profiles WHERE user_id = ?",
        (user_pk,),
    ).fetchone()
    return row or {"timezone": DEFAULT_USER_TIMEZONE}


def _timezone_for_row(row: sqlite3.Row | dict[str, Any] | None) -> str:
    if row is None:
        return DEFAULT_USER_TIMEZONE
    if isinstance(row, sqlite3.Row):
        return str(row["user_timezone"] if "user_timezone" in row.keys() else DEFAULT_USER_TIMEZONE)
    return str(row.get("user_timezone", row.get("timezone", DEFAULT_USER_TIMEZONE)))


def get_user_timezone(user_id: str) -> str:
    """Return the authenticated user's configured IANA timezone."""

    external_key = _validate_user_id(user_id)
    connection = get_connection()
    try:
        user_pk = _ensure_user(connection, external_key)
        connection.commit()
        return str(_user_profile(connection, user_pk)["timezone"])
    finally:
        connection.close()


def set_user_timezone(user_id: str, timezone_name: str) -> str:
    """Set and return the authenticated user's IANA timezone."""

    external_key = _validate_user_id(user_id)
    timezone_name = _validate_timezone(timezone_name)
    connection = get_connection()
    try:
        user_pk = _ensure_user(connection, external_key)
        connection.execute(
            """
            UPDATE user_profiles
            SET timezone = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE user_id = ?
            """,
            (timezone_name, user_pk),
        )
        connection.commit()
        return timezone_name
    finally:
        connection.close()


def _get_task_row(
    connection: sqlite3.Connection,
    user_pk: int,
    task_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT tasks.*, users.external_key AS user_external_key
               , user_profiles.timezone AS user_timezone
        FROM tasks
        JOIN users ON users.id = tasks.user_id
        JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE tasks.id = ?
          AND tasks.user_id = ?
          AND tasks.deleted_at IS NULL
        """,
        (task_id, user_pk),
    ).fetchone()


def _task_select_sql() -> str:
    return """
        SELECT tasks.*, users.external_key AS user_external_key
               , user_profiles.timezone AS user_timezone
        FROM tasks
        JOIN users ON users.id = tasks.user_id
        JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE tasks.user_id = ?
          AND tasks.deleted_at IS NULL
          AND tasks.status <> 'archived'
    """


def add_task(
    user_id: str,
    title: str,
    description: Optional[str] = None,
    *,
    planned_start_at: str | None = None,
    due_at: str | None = None,
    timezone_name: str | None = None,
    source: str = "manual",
    request_id: str | None = None,
    include_metadata: bool = False,
) -> dict[str, Any]:
    """Create a task; duplicate titles remain valid independent tasks."""

    external_key = _validate_user_id(user_id)
    title = _validate_title(title)
    description = _validate_description(description)
    source = _validate_source(source)
    if request_id is not None:
        request_id = str(request_id).strip()
        if not 1 <= len(request_id) <= 200:
            raise ValueError("request_id must be between 1 and 200 characters")
    connection = get_connection()
    try:
        user_pk = _ensure_user(connection, external_key)
        user_timezone = _validate_timezone(
            timezone_name or str(_user_profile(connection, user_pk)["timezone"])
        )
        cursor = connection.execute(
            """
            INSERT INTO tasks(
                user_id, title, description, planned_start_at, due_at, source, request_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_pk,
                title,
                description,
                _normalize_timestamp(
                    planned_start_at,
                    timezone_name=user_timezone,
                    field_name="planned_start_at",
                ),
                _normalize_timestamp(
                    due_at,
                    timezone_name=user_timezone,
                    field_name="due_at",
                ),
                source,
                request_id,
            ),
        )
        task_id = cursor.lastrowid
        connection.commit()
        row = _get_task_row(connection, user_pk, task_id)
        return serialize_task(
            row,
            _timezone_for_row(row),
            include_metadata=include_metadata,
        )
    finally:
        connection.close()


def get_all_tasks(
    user_id: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Return active tasks for one user with optional bounded pagination."""

    external_key = _validate_user_id(user_id)
    if offset < 0 or (limit is not None and not 1 <= limit <= 500):
        raise ValueError("offset must be non-negative and limit must be 1..500")
    connection = get_connection()
    try:
        user_row = connection.execute(
            "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
            (external_key,),
        ).fetchone()
        if user_row is None:
            return []
        sql = _task_select_sql() + " ORDER BY tasks.created_at DESC, tasks.id DESC"
        params: list[Any] = [user_row["id"]]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        rows = connection.execute(sql, params).fetchall()
        return [serialize_task(row, _timezone_for_row(row)) for row in rows]
    finally:
        connection.close()


def get_task_by_id(user_id: str, task_id: int) -> Optional[dict[str, Any]]:
    """Return a non-archived task only when it belongs to the user."""

    external_key = _validate_user_id(user_id)
    connection = get_connection()
    try:
        row = connection.execute(
            "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
            (external_key,),
        ).fetchone()
        if row is None:
            return None
        task = _get_task_row(connection, int(row["id"]), task_id)
        return serialize_task(task, _timezone_for_row(task))
    finally:
        connection.close()


def update_task(
    user_id: str,
    task_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    *,
    planned_start_at: str | None = None,
    due_at: str | None = None,
) -> Optional[dict[str, Any]]:
    """Update owned task fields and enforce the documented state machine."""

    external_key = _validate_user_id(user_id)
    if title is not None:
        title = _validate_title(title)
    if description is not None:
        description = _validate_description(description)
    if status is not None and status not in TASK_STATUSES:
        raise InvalidTaskStatusError(f"unsupported task status: {status}")

    connection = get_connection()
    try:
        user_pk = _ensure_user(connection, external_key)
        current = _get_task_row(connection, user_pk, task_id)
        if current is None:
            return None
        current_status = current["status"]
        if status is not None and status != current_status:
            if status not in ALLOWED_STATUS_TRANSITIONS[current_status]:
                raise InvalidTaskStatusError(
                    f"cannot change task status from {current_status} to {status}"
                )

        user_timezone = str(_user_profile(connection, user_pk)["timezone"])
        values = {
            "title": title if title is not None else current["title"],
            "description": (
                description if description is not None else current["description"]
            ),
            "status": status if status is not None else current_status,
            "planned_start_at": (
                _normalize_timestamp(
                    planned_start_at,
                    timezone_name=user_timezone,
                    field_name="planned_start_at",
                )
                if planned_start_at is not None
                else current["planned_start_at"]
            ),
            "due_at": (
                _normalize_timestamp(
                    due_at,
                    timezone_name=user_timezone,
                    field_name="due_at",
                )
                if due_at is not None
                else current["due_at"]
            ),
        }
        connection.execute(
            """
            UPDATE tasks
            SET title = ?, description = ?, status = ?,
                planned_start_at = ?, due_at = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                deleted_at = CASE WHEN ? = 'archived'
                                  THEN strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                                  ELSE deleted_at END
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (
                values["title"],
                values["description"],
                values["status"],
                values["planned_start_at"],
                values["due_at"],
                values["status"],
                task_id,
                user_pk,
            ),
        )
        if status is not None:
            from database.plans import refresh_plan_progress_for_task

            refresh_plan_progress_for_task(connection, user_pk, task_id)
        connection.commit()
        updated = _get_task_row(connection, user_pk, task_id)
        return serialize_task(updated, _timezone_for_row(updated))
    finally:
        connection.close()


def archive_task(user_id: str, task_id: int) -> bool:
    """Archive a cancelled task through the same state-machine guard."""

    task = get_task_by_id(user_id, task_id)
    if task is None:
        return False
    update_task(user_id, task_id, status="archived")
    return True


def delete_task(user_id: str, task_id: int) -> bool:
    """Archive an owned task without physically deleting its history.

    Archival is a dedicated delete command. Generic status edits still reject
    arbitrary transitions to ``archived`` and follow the documented state
    machine.
    """

    external_key = _validate_user_id(user_id)
    connection = get_connection()
    try:
        user_pk = _ensure_user(connection, external_key)
        row = _get_task_row(connection, user_pk, task_id)
        if row is None:
            return False
        connection.execute(
            """
            UPDATE tasks
            SET status = 'archived',
                deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (task_id, user_pk),
        )
        connection.commit()
        return True
    finally:
        connection.close()


def search_tasks(
    user_id: str,
    query: str,
    *,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Search only active tasks belonging to the authenticated user."""

    external_key = _validate_user_id(user_id)
    query = str(query).strip()
    if not query:
        raise ValueError("query must not be blank")
    if offset < 0 or (limit is not None and not 1 <= limit <= 500):
        raise ValueError("offset must be non-negative and limit must be 1..500")
    connection = get_connection()
    try:
        user_row = connection.execute(
            "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
            (external_key,),
        ).fetchone()
        if user_row is None:
            return []
        escaped_query = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped_query}%"
        sql = _task_select_sql() + """
            AND (
                tasks.title LIKE ? ESCAPE '\\'
                OR tasks.description LIKE ? ESCAPE '\\'
            )
            ORDER BY tasks.created_at DESC, tasks.id DESC
        """
        params: list[Any] = [user_row["id"], pattern, pattern]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        rows = connection.execute(sql, params).fetchall()
        return [serialize_task(row, _timezone_for_row(row)) for row in rows]
    finally:
        connection.close()


OCCURRENCE_STATUSES = frozenset({"started", "completed", "cancelled"})


def _serialize_occurrence(
    occurrence: sqlite3.Row | None,
    timezone_name: str = DEFAULT_USER_TIMEZONE,
) -> Optional[dict[str, Any]]:
    """Return an occurrence using the user's display timezone."""

    if occurrence is None:
        return None
    data = dict(occurrence)
    if "user_external_key" in data:
        data["user_id"] = data.pop("user_external_key")
    data.pop("user_timezone", None)
    for field in (
        "planned_at",
        "started_at",
        "completed_at",
        "occurred_at",
        "created_at",
    ):
        if field in data:
            data[field] = convert_utc_to_local(data[field], timezone_name)
    return data


def _get_occurrence_row(
    connection: sqlite3.Connection,
    user_pk: int,
    occurrence_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT task_occurrences.*,
               users.external_key AS user_external_key,
               user_profiles.timezone AS user_timezone
        FROM task_occurrences
        JOIN users ON users.id = task_occurrences.user_id
        JOIN user_profiles ON user_profiles.user_id = users.id
        WHERE task_occurrences.id = ? AND task_occurrences.user_id = ?
        """,
        (occurrence_id, user_pk),
    ).fetchone()


def record_task_occurrence(
    user_id: str,
    task_id: int,
    *,
    status: str = "completed",
    planned_at: str | datetime | None = None,
    started_at: str | datetime | None = None,
    completed_at: str | datetime | None = None,
    occurred_at: str | datetime | None = None,
    source: str = "agent",
    notes: str | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Record one execution and update its task atomically.

    Reusing an idempotency key returns the original occurrence and does not
    increment execution history a second time.
    """

    external_key = _validate_user_id(user_id)
    if status not in OCCURRENCE_STATUSES:
        raise ValueError(f"unsupported occurrence status: {status}")
    source = str(source).strip()
    if not source or len(source) > 100:
        raise ValueError("source must be between 1 and 100 characters")
    if notes is not None:
        notes = str(notes)
        if len(notes) > 10000:
            raise ValueError("notes is too long")
    if idempotency_key is not None:
        idempotency_key = str(idempotency_key).strip()
        if not idempotency_key or len(idempotency_key) > 200:
            raise ValueError("idempotency_key must be between 1 and 200 characters")

    connection = get_connection()
    try:
        user_pk = _ensure_user(connection, external_key)
        user_timezone = str(_user_profile(connection, user_pk)["timezone"])
        connection.commit()

        if idempotency_key is not None:
            existing = connection.execute(
                """
                SELECT id FROM task_occurrences
                WHERE user_id = ? AND idempotency_key = ?
                """,
                (user_pk, idempotency_key),
            ).fetchone()
            if existing is not None:
                row = _get_occurrence_row(connection, user_pk, existing["id"])
                return _serialize_occurrence(row, _timezone_for_row(row))

        task = connection.execute(
            """
            SELECT id, status FROM tasks
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (task_id, user_pk),
        ).fetchone()
        if task is None:
            raise TaskNotFoundError("task does not belong to the current user")

        event_time = _normalize_timestamp(
            occurred_at,
            timezone_name=user_timezone,
            field_name="occurred_at",
        ) or _now_utc_timestamp(user_timezone)
        planned_value = _normalize_timestamp(
            planned_at,
            timezone_name=user_timezone,
            field_name="planned_at",
        )
        started_value = _normalize_timestamp(
            started_at,
            timezone_name=user_timezone,
            field_name="started_at",
        )
        completed_value = _normalize_timestamp(
            completed_at,
            timezone_name=user_timezone,
            field_name="completed_at",
        )
        if status == "started" and started_value is None:
            started_value = event_time
        if status == "completed" and completed_value is None:
            completed_value = event_time
        if started_value and completed_value:
            if _parse_timestamp(
                started_value,
                timezone_name="UTC",
                field_name="started_at",
            ) > _parse_timestamp(
                completed_value,
                timezone_name="UTC",
                field_name="completed_at",
            ):
                raise InvalidTimestampError("started_at must not be after completed_at")

        current_status = str(task["status"])
        target_status = {
            "started": "in_progress",
            "completed": "completed",
            "cancelled": "cancelled",
        }[status]
        if current_status != target_status:
            if target_status not in ALLOWED_STATUS_TRANSITIONS[current_status]:
                raise InvalidTaskStatusError(
                    f"cannot change task status from {current_status} to {target_status}"
                )

        connection.execute("BEGIN")
        cursor = connection.execute(
            """
            INSERT INTO task_occurrences(
                task_id, user_id, status, planned_at, started_at,
                completed_at, occurred_at, source, notes, updated_at, idempotency_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now'), ?)
            """,
            (
                task_id,
                user_pk,
                status,
                planned_value,
                started_value,
                completed_value,
                event_time,
                source,
                notes,
                idempotency_key,
            ),
        )
        if current_status != target_status:
            connection.execute(
                """
                UPDATE tasks
                SET status = ?,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ? AND user_id = ? AND deleted_at IS NULL
                """,
                (target_status, task_id, user_pk),
            )
        from database.plans import refresh_plan_progress_for_task

        refresh_plan_progress_for_task(connection, user_pk, task_id)
        # Goal counters are derived from the same committed occurrence.
        from database.m3 import refresh_goal_progress_for_task

        refresh_goal_progress_for_task(connection, user_pk, task_id)
        connection.commit()
        row = _get_occurrence_row(connection, user_pk, cursor.lastrowid)
        return _serialize_occurrence(row, _timezone_for_row(row))
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def get_task_occurrences(
    user_id: str,
    task_id: int,
    *,
    start_at: str | datetime | None = None,
    end_at: str | datetime | None = None,
    period: str = "custom",
    reference_at: str | datetime | None = None,
    status: str | None = None,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """List a user's execution history, optionally bounded by event time."""

    external_key = _validate_user_id(user_id)
    if offset < 0 or (limit is not None and not 1 <= limit <= 500):
        raise ValueError("offset must be non-negative and limit must be 1..500")
    if period not in {"day", "week", "custom"}:
        raise ValueError("period must be day, week, or custom")
    if status is not None and status not in OCCURRENCE_STATUSES:
        raise ValueError(f"unsupported occurrence status: {status}")
    connection = get_connection()
    try:
        user_row = connection.execute(
            "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
            (external_key,),
        ).fetchone()
        if user_row is None:
            return []
        user_pk = int(user_row["id"])
        profile = _user_profile(connection, user_pk)
        timezone_name = str(profile["timezone"])
        if period != "custom":
            local_zone = ZoneInfo(_validate_timezone(timezone_name))
            if reference_at is None:
                reference_date = datetime.now(local_zone).date()
            elif isinstance(reference_at, datetime):
                reference_value = reference_at
                if reference_value.tzinfo is None:
                    reference_value = reference_value.replace(tzinfo=local_zone)
                reference_date = reference_value.astimezone(local_zone).date()
            else:
                text = str(reference_at).strip()
                try:
                    reference_date = date.fromisoformat(text[:10])
                except ValueError as error:
                    raise InvalidTimestampError(
                        "reference_at must be an ISO date or timestamp"
                    ) from error
            if period == "week":
                reference_date -= timedelta(days=reference_date.weekday())
            local_start = datetime.combine(reference_date, time.min, tzinfo=local_zone)
            local_end = local_start + timedelta(days=1 if period == "day" else 7)
            start_at = _format_utc_timestamp(local_start.astimezone(timezone.utc))
            end_at = _format_utc_timestamp(local_end.astimezone(timezone.utc))
        task = connection.execute(
            "SELECT id FROM tasks WHERE id = ? AND user_id = ?",
            (task_id, user_pk),
        ).fetchone()
        if task is None:
            return []
        sql = """
            SELECT task_occurrences.*,
                   users.external_key AS user_external_key,
                   user_profiles.timezone AS user_timezone
            FROM task_occurrences
            JOIN users ON users.id = task_occurrences.user_id
            JOIN user_profiles ON user_profiles.user_id = users.id
            WHERE task_occurrences.task_id = ?
              AND task_occurrences.user_id = ?
        """
        params: list[Any] = [task_id, user_pk]
        if start_at is not None:
            sql += " AND task_occurrences.occurred_at >= ?"
            params.append(
                _normalize_timestamp(
                    start_at,
                    timezone_name=timezone_name,
                    field_name="start_at",
                )
            )
        if end_at is not None:
            sql += " AND task_occurrences.occurred_at < ?"
            params.append(
                _normalize_timestamp(
                    end_at,
                    timezone_name=timezone_name,
                    field_name="end_at",
                )
            )
        if status is not None:
            sql += " AND task_occurrences.status = ?"
            params.append(status)
        sql += " ORDER BY task_occurrences.occurred_at DESC, task_occurrences.id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        rows = connection.execute(sql, params).fetchall()
        return [_serialize_occurrence(row, _timezone_for_row(row)) for row in rows]
    finally:
        connection.close()


def complete_task(
    user_id: str,
    task_id: int,
    *,
    occurred_at: str | datetime | None = None,
    notes: str | None = None,
    idempotency_key: str | None = None,
    source: str = "agent",
) -> dict[str, Any]:
    """Complete one execution of a task and create its history row."""

    return record_task_occurrence(
        user_id,
        task_id,
        status="completed",
        occurred_at=occurred_at,
        notes=notes,
        idempotency_key=idempotency_key,
        source=source,
    )


def transition_task(
    user_id: str,
    task_id: int,
    action: str,
    *,
    request_id: str | None = None,
    source: str = "agent",
    occurred_at: str | datetime | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Apply one explicit M8 task action and return its durable result."""

    action = str(action).strip().lower()
    occurrence_key = f"{request_id}:{action}" if request_id else None
    if action == "start":
        occurrence = record_task_occurrence(
            user_id,
            task_id,
            status="started",
            occurred_at=occurred_at,
            source=source,
            idempotency_key=occurrence_key,
        )
        return {"task": get_task_by_id(user_id, task_id), "occurrence": occurrence}
    if action == "complete":
        occurrence = complete_task(
            user_id,
            task_id,
            occurred_at=occurred_at,
            notes=notes,
            source=source,
            idempotency_key=occurrence_key,
        )
        return {"task": get_task_by_id(user_id, task_id), "occurrence": occurrence}
    if action == "cancel":
        occurrence = record_task_occurrence(
            user_id,
            task_id,
            status="cancelled",
            occurred_at=occurred_at,
            source=source,
            idempotency_key=occurrence_key,
        )
        return {"task": get_task_by_id(user_id, task_id), "occurrence": occurrence}
    if action == "pause":
        task = update_task(user_id, task_id, status="pending")
        if task is None:
            raise TaskNotFoundError("task does not belong to the current user")
        return {"task": task, "occurrence": None}
    if action == "archive":
        if not delete_task(user_id, task_id):
            raise TaskNotFoundError("task does not belong to the current user")
        return {"task_id": task_id, "status": "archived", "occurrence": None}
    raise InvalidTaskStatusError(
        "action must be start, pause, complete, cancel, or archive"
    )


_M3_EXPORTS = frozenset(
    {
        "GOAL_STATUSES",
        "RECURRENCE_FREQUENCIES",
        "REMINDER_STATUSES",
        "ReminderScheduler",
        "GoalNotFoundError",
        "RecurrenceNotFoundError",
        "ReminderNotFoundError",
        "ReminderStateError",
        "cancel_reminder",
        "claim_due_reminders",
        "create_goal_progress",
        "create_recurrence_rule",
        "create_reminder",
        "dispatch_due_reminders",
        "expire_due_reminders",
        "expire_reminder",
        "generate_recurrence_dates",
        "generate_recurrence_instances",
        "get_goal_progress",
        "get_recurrence_rule",
        "get_reminder",
        "list_goal_progress",
        "list_recurrence_rules",
        "list_reminders",
        "mark_reminder_failed",
        "mark_reminder_sent",
        "materialize_recurrence_reminders",
        "recover_stale_reminders",
        "refresh_goal_progress_for_task",
        "update_recurrence_rule",
        "update_reminder",
    }
)

_M4_EXPORTS = frozenset(
    {
        "AgentSessionNotFoundError",
        "LINK_BASES",
        "SESSION_STATUSES",
        "TRANSACTION_TYPES",
        "TaskTransactionLinkNotFoundError",
        "TransactionNotFoundError",
        "TransactionStateError",
        "archive_agent_session",
        "create_task_transaction_link",
        "create_transaction",
        "delete_task_transaction_link",
        "delete_transaction",
        "get_agent_session",
        "get_task_transaction_link",
        "get_transaction",
        "list_task_transaction_links",
        "list_tool_audits",
        "list_transactions",
        "query_statistics",
        "record_tool_audit",
        "remove_task_transaction_link",
        "statistics_query",
        "update_transaction",
        "upsert_agent_session",
        "void_transaction",
    }
)


def __getattr__(name: str) -> Any:
    """Lazily expose M3/M4 services without creating an import cycle."""

    if name in _M3_EXPORTS:
        from database import m3

        return getattr(m3, name)
    if name in _M4_EXPORTS:
        from database import m4

        return getattr(m4, name)
    raise AttributeError(name)
