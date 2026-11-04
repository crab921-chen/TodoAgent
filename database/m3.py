"""M3 reminder, recurrence, and cumulative-goal services.

The service keeps scheduling state in SQLite.  A scheduler may therefore be
stopped and restarted without losing reminders or creating a second send.
"""

from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from database import db


REMINDER_STATUSES = frozenset(
    {"scheduled", "processing", "sent", "failed", "cancelled", "expired"}
)
REMINDER_TERMINAL_STATUSES = frozenset({"sent", "cancelled", "expired"})
RECURRENCE_FREQUENCIES = frozenset({"daily", "weekly", "monthly", "custom"})
GOAL_STATUSES = frozenset({"active", "completed", "expired"})


class ReminderNotFoundError(LookupError):
    """The reminder is absent or does not belong to the current user."""


class ReminderStateError(ValueError):
    """A reminder cannot perform the requested state transition."""


class RecurrenceNotFoundError(LookupError):
    """The recurrence rule is absent or not owned by the current user."""


class GoalNotFoundError(LookupError):
    """The cumulative goal is absent or not owned by the current user."""


def _user_pk(connection: sqlite3.Connection, user_id: str, *, create: bool) -> int | None:
    external_key = db._validate_user_id(user_id)
    if create:
        return db._ensure_user(connection, external_key)
    row = connection.execute(
        "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
        (external_key,),
    ).fetchone()
    return None if row is None else int(row["id"])


def _timezone(
    connection: sqlite3.Connection,
    user_pk: int,
    timezone_name: str | None,
) -> str:
    if timezone_name is not None:
        return db._validate_timezone(timezone_name)
    return str(db._user_profile(connection, user_pk)["timezone"])


def _utc_text(value: datetime | str | None, *, field_name: str = "timestamp") -> str:
    if value is None:
        return db._format_utc_timestamp(datetime.now(timezone.utc))
    normalized = db._normalize_timestamp(
        value, timezone_name="UTC", field_name=field_name
    )
    if normalized is None:
        raise ValueError(f"{field_name} must not be null")
    return normalized


def _date_value(value: date | datetime | str, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO date") from error


def _display_timestamp(value: str | None, timezone_name: str) -> str | None:
    return db.convert_utc_to_local(value, timezone_name) if value else None


def _validate_source(source: str) -> str:
    value = str(source).strip()
    if not 1 <= len(value) <= 100:
        raise ValueError("source must be between 1 and 100 characters")
    return value


def _reminder_select() -> str:
    return """
        SELECT reminders.*,
               users.external_key AS user_external_key,
               user_profiles.timezone AS user_timezone
        FROM reminders
        JOIN users ON users.id = reminders.user_id
        JOIN user_profiles ON user_profiles.user_id = users.id
    """


def _reminder_row(
    connection: sqlite3.Connection,
    user_pk: int,
    reminder_id: int,
) -> sqlite3.Row | None:
    return connection.execute(
        _reminder_select()
        + " WHERE reminders.id = ? AND reminders.user_id = ?",
        (reminder_id, user_pk),
    ).fetchone()


def _serialize_reminder(
    row: sqlite3.Row | None,
    *,
    include_source: bool = False,
) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    user_timezone = str(data.pop("user_timezone", db.DEFAULT_USER_TIMEZONE))
    external_key = data.pop("user_external_key", None)
    if external_key is not None:
        data["user_id"] = external_key
    if not include_source:
        data.pop("source", None)
    reminder_timezone = str(data["timezone"])
    for field in ("trigger_at", "next_retry_at", "sent_at"):
        data[field] = _display_timestamp(data.get(field), reminder_timezone)
    for field in ("created_at", "updated_at"):
        data[field] = _display_timestamp(data.get(field), user_timezone)
    return data


def _task_belongs_to_user(
    connection: sqlite3.Connection,
    user_pk: int,
    task_id: int,
) -> bool:
    return connection.execute(
        """
        SELECT 1 FROM tasks
        WHERE id = ? AND user_id = ? AND deleted_at IS NULL
        """,
        (task_id, user_pk),
    ).fetchone() is not None


def create_reminder(
    user_id: str,
    content: str,
    trigger_at: str | datetime,
    *,
    task_id: int | None = None,
    timezone_name: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """Persist a scheduled reminder after normalizing its trigger to UTC."""

    content = str(content).strip()
    if not 1 <= len(content) <= 500:
        raise ValueError("content length must be between 1 and 500 characters")
    source = _validate_source(source)
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        reminder_timezone = _timezone(connection, user_pk, timezone_name)
        if task_id is not None and not _task_belongs_to_user(connection, user_pk, task_id):
            raise db.TaskNotFoundError("task does not belong to the current user")
        normalized_trigger = db._normalize_timestamp(
            trigger_at,
            timezone_name=reminder_timezone,
            field_name="trigger_at",
        )
        cursor = connection.execute(
            """
            INSERT INTO reminders(user_id, task_id, content, trigger_at, timezone, source)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user_pk, task_id, content, normalized_trigger, reminder_timezone, source),
        )
        connection.commit()
        row = _reminder_row(connection, user_pk, int(cursor.lastrowid))
        serialized = _serialize_reminder(row)
        if serialized is None:
            raise RuntimeError("created reminder could not be loaded")
        return serialized
    finally:
        connection.close()


def get_reminder(user_id: str, reminder_id: int) -> dict[str, Any] | None:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return None
        return _serialize_reminder(_reminder_row(connection, user_pk, reminder_id))
    finally:
        connection.close()


def list_reminders(
    user_id: str,
    *,
    status: str | None = None,
    start_at: str | datetime | None = None,
    end_at: str | datetime | None = None,
    limit: int | None = None,
    offset: int = 0,
    include_source: bool = False,
) -> list[dict[str, Any]]:
    if status is not None and status not in REMINDER_STATUSES:
        raise ValueError(f"unsupported reminder status: {status}")
    if offset < 0 or (limit is not None and not 1 <= limit <= 500):
        raise ValueError("offset must be non-negative and limit must be 1..500")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return []
        timezone_name = _timezone(connection, user_pk, None)
        sql = _reminder_select() + " WHERE reminders.user_id = ?"
        params: list[Any] = [user_pk]
        if status is not None:
            sql += " AND reminders.status = ?"
            params.append(status)
        if start_at is not None:
            sql += " AND reminders.trigger_at >= ?"
            params.append(
                db._normalize_timestamp(
                    start_at,
                    timezone_name=timezone_name,
                    field_name="start_at",
                )
            )
        if end_at is not None:
            sql += " AND reminders.trigger_at < ?"
            params.append(
                db._normalize_timestamp(
                    end_at,
                    timezone_name=timezone_name,
                    field_name="end_at",
                )
            )
        sql += " ORDER BY reminders.trigger_at ASC, reminders.id ASC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        reminders = []
        for row in connection.execute(sql, params).fetchall():
            serialized = _serialize_reminder(row, include_source=include_source)
            if serialized is not None:
                reminders.append(serialized)
        return reminders
    finally:
        connection.close()


def update_reminder(
    user_id: str,
    reminder_id: int,
    *,
    content: str | None = None,
    trigger_at: str | datetime | None = None,
    timezone_name: str | None = None,
) -> dict[str, Any]:
    """Update a not-yet-terminal reminder and reset failed delivery state."""

    if content is not None:
        content = str(content).strip()
        if not 1 <= len(content) <= 500:
            raise ValueError("content length must be between 1 and 500 characters")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            raise ReminderNotFoundError("reminder was not found")
        row = _reminder_row(connection, user_pk, reminder_id)
        if row is None:
            raise ReminderNotFoundError("reminder was not found")
        if row["status"] in REMINDER_TERMINAL_STATUSES or row["status"] == "processing":
            raise ReminderStateError(
                f"cannot update reminder in {row['status']} state"
            )
        reminder_timezone = _timezone(connection, user_pk, timezone_name or row["timezone"])
        normalized_trigger = (
            db._normalize_timestamp(
                trigger_at,
                timezone_name=reminder_timezone,
                field_name="trigger_at",
            )
            if trigger_at is not None
            else row["trigger_at"]
        )
        connection.execute(
            """
            UPDATE reminders
            SET content = COALESCE(?, content),
                trigger_at = ?,
                timezone = ?,
                status = 'scheduled',
                attempt_count = 0,
                last_error = NULL,
                next_retry_at = NULL,
                sent_at = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ?
            """,
            (content, normalized_trigger, reminder_timezone, reminder_id, user_pk),
        )
        connection.commit()
        serialized = _serialize_reminder(_reminder_row(connection, user_pk, reminder_id))
        if serialized is None:
            raise RuntimeError("updated reminder could not be loaded")
        return serialized
    finally:
        connection.close()


def cancel_reminder(user_id: str, reminder_id: int) -> bool:
    """Cancel a reminder unless it has already been sent or expired."""

    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        cursor = connection.execute(
            """
            UPDATE reminders
            SET status = 'cancelled',
                next_retry_at = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ?
              AND status IN ('scheduled', 'processing', 'failed')
            """,
            (reminder_id, user_pk),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def _recover_stale_on_connection(
    connection: sqlite3.Connection,
    *,
    cutoff: str,
    retry_at: str,
) -> int:
    cursor = connection.execute(
        """
        UPDATE reminders
        SET status = 'scheduled',
            next_retry_at = ?,
            updated_at = ?
        WHERE status = 'processing' AND updated_at < ?
        """,
        (retry_at, retry_at, cutoff),
    )
    return cursor.rowcount


def recover_stale_reminders(
    *,
    now: datetime | str | None = None,
    stale_after_seconds: int = 300,
) -> int:
    if stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be positive")
    now_text = _utc_text(now)
    now_dt = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    cutoff = db._format_utc_timestamp(
        now_dt - timedelta(seconds=stale_after_seconds)
    )
    connection = db.get_connection()
    try:
        count = _recover_stale_on_connection(
            connection, cutoff=cutoff, retry_at=now_text
        )
        connection.commit()
        return count
    finally:
        connection.close()


def claim_due_reminders(
    user_id: str | None = None,
    *,
    now: datetime | str | None = None,
    limit: int = 100,
    stale_after_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Atomically claim due reminders by changing them to ``processing``."""

    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    if stale_after_seconds < 1:
        raise ValueError("stale_after_seconds must be positive")
    now_text = _utc_text(now)
    now_dt = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    cutoff = db._format_utc_timestamp(
        now_dt - timedelta(seconds=stale_after_seconds)
    )
    connection = db.get_connection()
    try:
        connection.execute("BEGIN IMMEDIATE")
        _recover_stale_on_connection(
            connection, cutoff=cutoff, retry_at=now_text
        )
        params: list[Any] = [now_text, now_text]
        user_clause = ""
        if user_id is not None:
            user_pk = _user_pk(connection, user_id, create=False)
            if user_pk is None:
                connection.rollback()
                return []
            user_clause = " AND user_id = ?"
            params.append(user_pk)
        candidates = connection.execute(
            f"""
            SELECT id, status FROM reminders
            WHERE (
                (status = 'scheduled' AND trigger_at <= ?)
                OR (status = 'failed' AND next_retry_at IS NOT NULL AND next_retry_at <= ?)
            ){user_clause}
            ORDER BY trigger_at ASC, id ASC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
        ids: list[int] = []
        for candidate in candidates:
            cursor = connection.execute(
                """
                UPDATE reminders
                SET status = 'processing',
                    attempt_count = attempt_count + 1,
                    updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (now_text, candidate["id"], candidate["status"]),
            )
            if cursor.rowcount:
                ids.append(int(candidate["id"]))
        rows = []
        for reminder_id in ids:
            row = connection.execute(
                _reminder_select() + " WHERE reminders.id = ?",
                (reminder_id,),
            ).fetchone()
            serialized = _serialize_reminder(row)
            if serialized is not None:
                rows.append(serialized)
        connection.commit()
        return rows
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def mark_reminder_sent(
    user_id: str,
    reminder_id: int,
    *,
    sent_at: datetime | str | None = None,
) -> bool:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        sent_text = _utc_text(sent_at, field_name="sent_at")
        cursor = connection.execute(
            """
            UPDATE reminders
            SET status = 'sent', sent_at = ?, next_retry_at = NULL,
                last_error = NULL, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'processing'
            """,
            (sent_text, sent_text, reminder_id, user_pk),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def mark_reminder_failed(
    user_id: str,
    reminder_id: int,
    error: str,
    *,
    max_attempts: int = 3,
    retry_delay_seconds: int = 60,
    failed_at: datetime | str | None = None,
) -> bool:
    if max_attempts < 1 or retry_delay_seconds < 0:
        raise ValueError("max_attempts must be positive and retry delay non-negative")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        row = connection.execute(
            "SELECT attempt_count FROM reminders WHERE id = ? AND user_id = ? AND status = 'processing'",
            (reminder_id, user_pk),
        ).fetchone()
        if row is None:
            return False
        failed_text = _utc_text(failed_at, field_name="failed_at")
        attempts = int(row["attempt_count"])
        next_retry = None
        if attempts < max_attempts:
            next_retry = db._format_utc_timestamp(
                datetime.fromisoformat(failed_text.replace("Z", "+00:00"))
                + timedelta(seconds=retry_delay_seconds * (2 ** (attempts - 1)))
            )
        cursor = connection.execute(
            """
            UPDATE reminders
            SET status = 'failed', last_error = ?, next_retry_at = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = 'processing'
            """,
            (str(error)[:1000], next_retry, failed_text, reminder_id, user_pk),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def expire_due_reminders(
    *,
    now: datetime | str | None = None,
    max_age_seconds: int = 86400,
    user_id: str | None = None,
) -> int:
    """Mark old unsent reminders expired so they cannot be delivered later."""

    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be non-negative")
    now_text = _utc_text(now)
    now_dt = datetime.fromisoformat(now_text.replace("Z", "+00:00"))
    cutoff = db._format_utc_timestamp(now_dt - timedelta(seconds=max_age_seconds))
    connection = db.get_connection()
    try:
        params: list[Any] = [now_text, cutoff]
        user_clause = ""
        if user_id is not None:
            user_pk = _user_pk(connection, user_id, create=False)
            if user_pk is None:
                return 0
            user_clause = " AND user_id = ?"
            params.append(user_pk)
        cursor = connection.execute(
            f"""
            UPDATE reminders
            SET status = 'expired', next_retry_at = NULL, updated_at = ?
            WHERE status IN ('scheduled', 'failed') AND trigger_at < ?{user_clause}
            """,
            params,
        )
        connection.commit()
        return cursor.rowcount
    finally:
        connection.close()


def expire_reminder(user_id: str, reminder_id: int) -> bool:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        cursor = connection.execute(
            """
            UPDATE reminders SET status = 'expired', next_retry_at = NULL,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ? AND status IN ('scheduled', 'failed')
            """,
            (reminder_id, user_pk),
        )
        connection.commit()
        return cursor.rowcount > 0
    finally:
        connection.close()


def dispatch_due_reminders(
    sender: Callable[[dict[str, Any]], Any],
    *,
    user_id: str | None = None,
    now: datetime | str | None = None,
    limit: int = 100,
    max_attempts: int = 3,
    retry_delay_seconds: int = 60,
    stale_after_seconds: int = 300,
) -> list[dict[str, Any]]:
    """Claim, deliver, and persist the result of each due reminder."""

    claimed = claim_due_reminders(
        user_id,
        now=now,
        limit=limit,
        stale_after_seconds=stale_after_seconds,
    )
    results: list[dict[str, Any]] = []
    for reminder in claimed:
        current = get_reminder(str(reminder["user_id"]), int(reminder["id"]))
        if current is None or current["status"] != "processing":
            results.append(
                {
                    "reminder": reminder,
                    "status": current["status"] if current else "cancelled",
                }
            )
            continue
        try:
            sender(reminder)
        except Exception as error:  # delivery failure is durable business state
            failed = mark_reminder_failed(
                str(reminder["user_id"]),
                int(reminder["id"]),
                str(error),
                max_attempts=max_attempts,
                retry_delay_seconds=retry_delay_seconds,
                failed_at=now,
            )
            results.append(
                {
                    "reminder": reminder,
                    "status": "failed" if failed else "cancelled",
                    "error": str(error) if failed else None,
                }
            )
        else:
            sent = mark_reminder_sent(
                str(reminder["user_id"]), int(reminder["id"]), sent_at=now
            )
            results.append(
                {
                    "reminder": reminder,
                    "status": "sent" if sent else "cancelled",
                }
            )
    return results


def create_recurrence_rule(
    user_id: str,
    task_id: int,
    frequency: str,
    start_date: date | datetime | str,
    *,
    interval_value: int = 1,
    end_date: date | datetime | str | None = None,
    timezone_name: str | None = None,
    active: bool = True,
) -> dict[str, Any]:
    frequency = str(frequency).strip().lower()
    if frequency not in RECURRENCE_FREQUENCIES:
        raise ValueError("frequency must be daily, weekly, monthly, or custom")
    if interval_value <= 0:
        raise ValueError("interval_value must be positive")
    start = _date_value(start_date, field_name="start_date")
    end = None if end_date is None else _date_value(end_date, field_name="end_date")
    if end is not None and end < start:
        raise ValueError("end_date must not be before start_date")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        if not _task_belongs_to_user(connection, user_pk, task_id):
            raise db.TaskNotFoundError("task does not belong to the current user")
        rule_timezone = _timezone(connection, user_pk, timezone_name)
        cursor = connection.execute(
            """
            INSERT INTO recurrence_rules(
                user_id, task_id, frequency, interval_value,
                start_date, end_date, timezone, active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_pk, task_id, frequency, interval_value, start.isoformat(),
             end.isoformat() if end else None, rule_timezone, int(active)),
        )
        connection.commit()
        rule = get_recurrence_rule(str(user_id), int(cursor.lastrowid))
        if rule is None:
            raise RuntimeError("created recurrence rule could not be loaded")
        return rule
    finally:
        connection.close()


def _rule_row(connection: sqlite3.Connection, user_pk: int, rule_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM recurrence_rules WHERE id = ? AND user_id = ?",
        (rule_id, user_pk),
    ).fetchone()


def _serialize_rule(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def get_recurrence_rule(user_id: str, rule_id: int) -> dict[str, Any] | None:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        return None if user_pk is None else _serialize_rule(_rule_row(connection, user_pk, rule_id))
    finally:
        connection.close()


def list_recurrence_rules(
    user_id: str,
    *,
    active: bool | None = None,
    task_id: int | None = None,
) -> list[dict[str, Any]]:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return []
        sql = "SELECT * FROM recurrence_rules WHERE user_id = ?"
        params: list[Any] = [user_pk]
        if active is not None:
            sql += " AND active = ?"
            params.append(int(active))
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY start_date ASC, id ASC"
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


def update_recurrence_rule(
    user_id: str,
    rule_id: int,
    *,
    frequency: str | None = None,
    interval_value: int | None = None,
    start_date: date | datetime | str | None = None,
    end_date: date | datetime | str | None = None,
    timezone_name: str | None = None,
    active: bool | None = None,
) -> dict[str, Any]:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            raise RecurrenceNotFoundError("recurrence rule was not found")
        row = _rule_row(connection, user_pk, rule_id)
        if row is None:
            raise RecurrenceNotFoundError("recurrence rule was not found")
        new_frequency = str(frequency or row["frequency"]).lower()
        if new_frequency not in RECURRENCE_FREQUENCIES:
            raise ValueError("unsupported recurrence frequency")
        new_interval = int(
            row["interval_value"] if interval_value is None else interval_value
        )
        if new_interval <= 0:
            raise ValueError("interval_value must be positive")
        new_start = _date_value(start_date or row["start_date"], field_name="start_date")
        new_end = _date_value(end_date, field_name="end_date") if end_date is not None else (
            _date_value(row["end_date"], field_name="end_date") if row["end_date"] else None
        )
        if new_end is not None and new_end < new_start:
            raise ValueError("end_date must not be before start_date")
        new_timezone = _timezone(connection, user_pk, timezone_name or row["timezone"])
        connection.execute(
            """
            UPDATE recurrence_rules
            SET frequency = ?, interval_value = ?, start_date = ?, end_date = ?,
                timezone = ?, active = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ?
            """,
            (new_frequency, new_interval, new_start.isoformat(),
             new_end.isoformat() if new_end else None, new_timezone,
             int(row["active"] if active is None else active), rule_id, user_pk),
        )
        connection.commit()
        rule = get_recurrence_rule(user_id, rule_id)
        if rule is None:
            raise RuntimeError("updated recurrence rule could not be loaded")
        return rule
    finally:
        connection.close()


def _add_months(value: date, months: int, original_day: int) -> date:
    month_index = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(month_index, 12)
    month = month_index + 1
    return date(year, month, min(original_day, calendar.monthrange(year, month)[1]))


def generate_recurrence_dates(
    rule: dict[str, Any],
    *,
    range_start: date | str | None = None,
    range_end: date | str | None = None,
) -> list[date]:
    """Generate deterministic local calendar dates without creating fake history."""

    if not int(rule.get("active", 1)):
        return []
    start = _date_value(rule["start_date"], field_name="start_date")
    rule_end = _date_value(rule["end_date"], field_name="end_date") if rule.get("end_date") else None
    lower = _date_value(range_start, field_name="range_start") if range_start else start
    upper = _date_value(range_end, field_name="range_end") if range_end else (rule_end or lower + timedelta(days=366))
    if rule_end is not None:
        upper = min(upper, rule_end)
    current = start
    original_day = start.day
    interval = int(rule["interval_value"])
    dates: list[date] = []
    while current <= upper:
        if current >= lower:
            dates.append(current)
        if rule["frequency"] in ("daily", "custom"):
            current += timedelta(days=interval)
        elif rule["frequency"] == "weekly":
            current += timedelta(weeks=interval)
        elif rule["frequency"] == "monthly":
            current = _add_months(current, interval, original_day)
        else:
            raise ValueError("unsupported recurrence frequency")
    return dates


def generate_recurrence_instances(
    user_id: str,
    rule_id: int,
    *,
    range_start: date | str | None = None,
    range_end: date | str | None = None,
) -> list[dict[str, Any]]:
    rule = get_recurrence_rule(user_id, rule_id)
    if rule is None:
        raise RecurrenceNotFoundError("recurrence rule was not found")
    return [
        {
            "rule_id": rule_id,
            "task_id": rule["task_id"],
            "instance_date": current.isoformat(),
            "timezone": rule["timezone"],
            "idempotency_key": f"recurrence:{rule_id}:{current.isoformat()}",
        }
        for current in generate_recurrence_dates(
            rule, range_start=range_start, range_end=range_end
        )
    ]


def materialize_recurrence_reminders(
    user_id: str,
    rule_id: int,
    *,
    range_start: date | str | None = None,
    range_end: date | str | None = None,
    content: str | None = None,
    local_time: str = "09:00",
) -> list[dict[str, Any]]:
    """Create at-most-one reminder for each recurrence date."""

    rule = get_recurrence_rule(user_id, rule_id)
    if rule is None:
        raise RecurrenceNotFoundError("recurrence rule was not found")
    try:
        hour, minute = (int(part) for part in local_time.split(":", 1))
        local_clock = time(hour, minute)
    except (ValueError, TypeError) as error:
        raise ValueError("local_time must be HH:MM") from error
    if not (0 <= local_clock.hour <= 23 and 0 <= local_clock.minute <= 59):
        raise ValueError("local_time must be HH:MM")
    zone = ZoneInfo(rule["timezone"])
    result: list[dict[str, Any]] = []
    for current in generate_recurrence_dates(rule, range_start=range_start, range_end=range_end):
        trigger = datetime.combine(current, local_clock, tzinfo=zone)
        reminder_content = content or f"Recurring task #{rule['task_id']}"
        connection = db.get_connection()
        try:
            user_pk = _user_pk(connection, user_id, create=False)
            assert user_pk is not None
            trigger_text = db._format_utc_timestamp(trigger)
            existing = connection.execute(
                """
                SELECT id FROM reminders
                WHERE user_id = ? AND task_id = ? AND trigger_at = ?
                """,
                (user_pk, rule["task_id"], trigger_text),
            ).fetchone()
        finally:
            connection.close()
        if existing is None:
            result.append(create_reminder(
                user_id,
                reminder_content,
                trigger.isoformat(),
                task_id=int(rule["task_id"]),
                timezone_name=rule["timezone"],
            ))
    return result


def _goal_select() -> str:
    return "SELECT * FROM goal_progress"


def _goal_row(connection: sqlite3.Connection, user_pk: int, goal_id: int) -> sqlite3.Row | None:
    return connection.execute(
        _goal_select() + " WHERE id = ? AND user_id = ?", (goal_id, user_pk)
    ).fetchone()


def _goal_count(
    connection: sqlite3.Connection,
    user_pk: int,
    task_id: int,
    period_start: str,
    period_end: str,
    timezone_name: str,
) -> int:
    start_text = db._normalize_timestamp(
        f"{period_start}T00:00:00", timezone_name=timezone_name, field_name="period_start"
    )
    end_date = _date_value(period_end, field_name="period_end") + timedelta(days=1)
    end_text = db._normalize_timestamp(
        f"{end_date.isoformat()}T00:00:00", timezone_name=timezone_name, field_name="period_end"
    )
    return int(connection.execute(
        """
        SELECT COUNT(*) FROM task_occurrences
        WHERE user_id = ? AND task_id = ? AND status = 'completed'
          AND deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?
        """,
        (user_pk, task_id, start_text, end_text),
    ).fetchone()[0])


def _refresh_goal_row(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    now: datetime | None = None,
) -> None:
    user_timezone = str(db._user_profile(connection, int(row["user_id"]))["timezone"])
    count = _goal_count(
        connection, int(row["user_id"]), int(row["task_id"]),
        row["period_start"], row["period_end"], user_timezone,
    )
    current = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo(user_timezone)).date()
    end = _date_value(row["period_end"], field_name="period_end")
    if count >= int(row["target_count"]):
        status = "completed"
        completed_at = row["completed_at"] or db._format_utc_timestamp(now or datetime.now(timezone.utc))
    elif current > end:
        status = "expired"
        completed_at = None
    else:
        status = "active"
        completed_at = None
    connection.execute(
        """
        UPDATE goal_progress
        SET completed_count = ?, status = ?, completed_at = ?,
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        WHERE id = ?
        """,
        (count, status, completed_at, row["id"]),
    )


def refresh_goal_progress_for_task(
    connection: sqlite3.Connection,
    user_pk: int,
    task_id: int,
) -> None:
    for row in connection.execute(
        """
        SELECT * FROM goal_progress
        WHERE user_id = ? AND task_id = ? AND status <> 'expired'
        """,
        (user_pk, task_id),
    ).fetchall():
        _refresh_goal_row(connection, row)


def _serialize_goal(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def create_goal_progress(
    user_id: str,
    task_id: int,
    period_start: date | datetime | str,
    period_end: date | datetime | str,
    target_count: int,
) -> dict[str, Any]:
    start = _date_value(period_start, field_name="period_start")
    end = _date_value(period_end, field_name="period_end")
    if end <= start:
        raise ValueError("period_end must be after period_start")
    if target_count <= 0:
        raise ValueError("target_count must be positive")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        if not _task_belongs_to_user(connection, user_pk, task_id):
            raise db.TaskNotFoundError("task does not belong to the current user")
        cursor = connection.execute(
            """
            INSERT INTO goal_progress(user_id, task_id, period_start, period_end, target_count)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_pk, task_id, start.isoformat(), end.isoformat(), target_count),
        )
        row = _goal_row(connection, user_pk, int(cursor.lastrowid))
        assert row is not None
        _refresh_goal_row(connection, row)
        connection.commit()
        goal = _serialize_goal(_goal_row(connection, user_pk, int(cursor.lastrowid)))
        if goal is None:
            raise RuntimeError("created goal could not be loaded")
        return goal
    finally:
        connection.close()


def get_goal_progress(user_id: str, goal_id: int) -> dict[str, Any] | None:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return None
        row = _goal_row(connection, user_pk, goal_id)
        if row is None:
            return None
        _refresh_goal_row(connection, row)
        connection.commit()
        return _serialize_goal(_goal_row(connection, user_pk, goal_id))
    finally:
        connection.close()


def list_goal_progress(
    user_id: str,
    *,
    task_id: int | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    if status is not None and status not in GOAL_STATUSES:
        raise ValueError(f"unsupported goal status: {status}")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return []
        sql = _goal_select() + " WHERE user_id = ?"
        params: list[Any] = [user_pk]
        if task_id is not None:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY period_start ASC, id ASC"
        rows = connection.execute(sql, params).fetchall()
        for row in rows:
            _refresh_goal_row(connection, row)
        connection.commit()
        refreshed = [dict(row) for row in connection.execute(sql, params).fetchall()]
        if status is not None:
            refreshed = [row for row in refreshed if row["status"] == status]
        return refreshed
    finally:
        connection.close()


class ReminderScheduler:
    """Deterministic scheduler facade for startup and one-shot worker runs."""

    def __init__(
        self,
        sender: Callable[[dict[str, Any]], Any],
        *,
        max_attempts: int = 3,
        retry_delay_seconds: int = 60,
        stale_after_seconds: int = 300,
        expire_after_seconds: int = 86400,
    ) -> None:
        self.sender = sender
        self.max_attempts = max_attempts
        self.retry_delay_seconds = retry_delay_seconds
        self.stale_after_seconds = stale_after_seconds
        self.expire_after_seconds = expire_after_seconds

    def recover(self, *, now: datetime | str | None = None) -> int:
        return recover_stale_reminders(
            now=now, stale_after_seconds=self.stale_after_seconds
        )

    def run_once(
        self,
        *,
        user_id: str | None = None,
        now: datetime | str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        expire_due_reminders(
            now=now,
            max_age_seconds=self.expire_after_seconds,
            user_id=user_id,
        )
        self.recover(now=now)
        return dispatch_due_reminders(
            self.sender,
            user_id=user_id,
            now=now,
            limit=limit,
            max_attempts=self.max_attempts,
            retry_delay_seconds=self.retry_delay_seconds,
            stale_after_seconds=self.stale_after_seconds,
        )


__all__ = [
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
]
