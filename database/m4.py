"""M4 finance, explicit task links, statistics, and runtime audit services."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from database import db


TRANSACTION_TYPES = frozenset({"expense", "income"})
LINK_BASES = frozenset({"explicit", "user_confirmed"})
SESSION_STATUSES = frozenset({"active", "archived"})
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")


class TransactionNotFoundError(LookupError):
    """The transaction is absent or not owned by the current user."""


class TransactionStateError(ValueError):
    """The requested transaction state change is not allowed."""


class TaskTransactionLinkNotFoundError(LookupError):
    """The task/transaction link is absent or not owned by the current user."""


class AgentSessionNotFoundError(LookupError):
    """The session is absent or not owned by the current user."""


def _user_pk(connection: sqlite3.Connection, user_id: str, *, create: bool) -> int | None:
    external_key = db._validate_user_id(user_id)
    if create:
        return db._ensure_user(connection, external_key)
    row = connection.execute(
        "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
        (external_key,),
    ).fetchone()
    return None if row is None else int(row["id"])


def _timezone(connection: sqlite3.Connection, user_pk: int) -> str:
    return str(db._user_profile(connection, user_pk)["timezone"])


def _default_currency(connection: sqlite3.Connection, user_pk: int) -> str:
    row = connection.execute(
        "SELECT default_currency FROM user_profiles WHERE user_id = ?",
        (user_pk,),
    ).fetchone()
    return str(row["default_currency"] if row else "CNY").upper()


def _required_text(value: Any, field_name: str, *, max_length: int) -> str:
    text = str(value).strip()
    if not text or len(text) > max_length:
        raise ValueError(f"{field_name} must be between 1 and {max_length} characters")
    return text


def _currency(value: str | None, connection: sqlite3.Connection, user_pk: int) -> str:
    normalized = (
        _default_currency(connection, user_pk) if value is None else str(value).strip().upper()
    )
    if not _CURRENCY_RE.fullmatch(normalized):
        raise ValueError("currency must be a three-letter ISO-style code")
    return normalized


def _amount_minor(value: Any) -> int:
    if type(value) is not int or value <= 0 or value > 9223372036854775807:
        raise ValueError("amount_minor must be a positive 64-bit integer")
    return value


def _utc_timestamp(
    value: datetime | str | None,
    *,
    timezone_name: str,
    field_name: str,
) -> str:
    normalized = db._normalize_timestamp(
        value,
        timezone_name=timezone_name,
        field_name=field_name,
    )
    return normalized or db._now_utc_timestamp(timezone_name)


def _display_timestamp(value: str | None, timezone_name: str) -> str | None:
    return db.convert_utc_to_local(value, timezone_name) if value else None


def _transaction_select() -> str:
    return """
        SELECT transactions.*,
               users.external_key AS user_external_key,
               user_profiles.timezone AS user_timezone
        FROM transactions
        JOIN users ON users.id = transactions.user_id
        JOIN user_profiles ON user_profiles.user_id = users.id
    """


def _transaction_row(
    connection: sqlite3.Connection,
    user_pk: int,
    transaction_id: int,
    *,
    include_deleted: bool = False,
) -> sqlite3.Row | None:
    sql = _transaction_select() + " WHERE transactions.id = ? AND transactions.user_id = ?"
    params: list[Any] = [transaction_id, user_pk]
    if not include_deleted:
        sql += " AND transactions.deleted_at IS NULL"
    return connection.execute(sql, params).fetchone()


def _serialize_transaction(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    user_timezone = str(data.pop("user_timezone", db.DEFAULT_USER_TIMEZONE))
    external_key = data.pop("user_external_key", None)
    if external_key is not None:
        data["user_id"] = external_key
    for field in ("occurred_at", "created_at", "updated_at", "deleted_at"):
        data[field] = _display_timestamp(data.get(field), user_timezone)
    return data


def create_transaction(
    user_id: str,
    transaction_type: str,
    amount_minor: int,
    category: str,
    *,
    currency: str | None = None,
    note: str | None = None,
    occurred_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Create an income or expense using integer minor currency units."""

    transaction_type = str(transaction_type).strip().lower()
    if transaction_type not in TRANSACTION_TYPES:
        raise ValueError("type must be expense or income")
    amount_minor = _amount_minor(amount_minor)
    category = _required_text(category, "category", max_length=100)
    if note is not None and len(str(note)) > 10000:
        raise ValueError("note is too long")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        user_timezone = _timezone(connection, user_pk)
        normalized_currency = _currency(currency, connection, user_pk)
        occurred_text = _utc_timestamp(
            occurred_at,
            timezone_name=user_timezone,
            field_name="occurred_at",
        )
        cursor = connection.execute(
            """
            INSERT INTO transactions(
                user_id, type, amount_minor, currency, category, note, occurred_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_pk,
                transaction_type,
                amount_minor,
                normalized_currency,
                category,
                None if note is None else str(note),
                occurred_text,
            ),
        )
        connection.commit()
        row = _transaction_row(connection, user_pk, int(cursor.lastrowid))
        result = _serialize_transaction(row)
        if result is None:
            raise RuntimeError("created transaction could not be loaded")
        return result
    finally:
        connection.close()


def get_transaction(
    user_id: str,
    transaction_id: int,
    *,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return None
        return _serialize_transaction(
            _transaction_row(
                connection,
                user_pk,
                int(transaction_id),
                include_deleted=include_deleted,
            )
        )
    finally:
        connection.close()


def list_transactions(
    user_id: str,
    *,
    transaction_type: str | None = None,
    category: str | None = None,
    currency: str | None = None,
    start_at: datetime | str | None = None,
    end_at: datetime | str | None = None,
    include_deleted: bool = False,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    if transaction_type is not None:
        transaction_type = str(transaction_type).strip().lower()
        if transaction_type not in TRANSACTION_TYPES:
            raise ValueError("type must be expense or income")
    if offset < 0 or (limit is not None and not 1 <= limit <= 500):
        raise ValueError("offset must be non-negative and limit must be 1..500")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return []
        timezone_name = _timezone(connection, user_pk)
        sql = _transaction_select() + " WHERE transactions.user_id = ?"
        params: list[Any] = [user_pk]
        if not include_deleted:
            sql += " AND transactions.deleted_at IS NULL"
        if transaction_type is not None:
            sql += " AND transactions.type = ?"
            params.append(transaction_type)
        if category is not None:
            sql += " AND transactions.category = ?"
            params.append(_required_text(category, "category", max_length=100))
        if currency is not None:
            params.append(_currency(currency, connection, user_pk))
            sql += " AND transactions.currency = ?"
        if start_at is not None:
            sql += " AND transactions.occurred_at >= ?"
            params.append(
                _utc_timestamp(
                    start_at,
                    timezone_name=timezone_name,
                    field_name="start_at",
                )
            )
        if end_at is not None:
            sql += " AND transactions.occurred_at < ?"
            params.append(
                _utc_timestamp(
                    end_at,
                    timezone_name=timezone_name,
                    field_name="end_at",
                )
            )
        sql += " ORDER BY transactions.occurred_at DESC, transactions.id DESC"
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params.extend((limit, offset))
        return [
            item
            for row in connection.execute(sql, params).fetchall()
            if (item := _serialize_transaction(row)) is not None
        ]
    finally:
        connection.close()


def update_transaction(
    user_id: str,
    transaction_id: int,
    *,
    transaction_type: str | None = None,
    amount_minor: int | None = None,
    currency: str | None = None,
    category: str | None = None,
    note: str | None = None,
    occurred_at: datetime | str | None = None,
) -> dict[str, Any] | None:
    if transaction_type is not None:
        transaction_type = str(transaction_type).strip().lower()
        if transaction_type not in TRANSACTION_TYPES:
            raise ValueError("type must be expense or income")
    if amount_minor is not None:
        _amount_minor(amount_minor)
    if category is not None:
        category = _required_text(category, "category", max_length=100)
    if note is not None and len(str(note)) > 10000:
        raise ValueError("note is too long")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return None
        current = _transaction_row(connection, user_pk, int(transaction_id))
        if current is None:
            return None
        timezone_name = _timezone(connection, user_pk)
        values = (
            transaction_type or current["type"],
            amount_minor if amount_minor is not None else current["amount_minor"],
            _currency(currency, connection, user_pk)
            if currency is not None
            else current["currency"],
            category if category is not None else current["category"],
            note if note is not None else current["note"],
            _utc_timestamp(occurred_at, timezone_name=timezone_name, field_name="occurred_at")
            if occurred_at is not None
            else current["occurred_at"],
        )
        connection.execute(
            """
            UPDATE transactions
            SET type = ?, amount_minor = ?, currency = ?, category = ?, note = ?,
                occurred_at = ?,
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (*values, int(transaction_id), user_pk),
        )
        connection.commit()
        return _serialize_transaction(_transaction_row(connection, user_pk, int(transaction_id)))
    finally:
        connection.close()


def void_transaction(user_id: str, transaction_id: int) -> bool:
    """Soft-delete a transaction so prior values remain auditable."""

    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        cursor = connection.execute(
            """
            UPDATE transactions
            SET deleted_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now'),
                updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ? AND deleted_at IS NULL
            """,
            (int(transaction_id), user_pk),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


delete_transaction = void_transaction


def _link_select() -> str:
    return """
        SELECT links.*, users.external_key AS user_external_key,
               tasks.title AS task_title,
               transactions.type AS transaction_type,
               transactions.amount_minor, transactions.currency,
               transactions.category, transactions.occurred_at,
               user_profiles.timezone AS user_timezone
        FROM task_transaction_links AS links
        JOIN users ON users.id = links.user_id
        JOIN user_profiles ON user_profiles.user_id = links.user_id
        JOIN tasks ON tasks.id = links.task_id AND tasks.user_id = links.user_id
        JOIN transactions
          ON transactions.id = links.transaction_id
         AND transactions.user_id = links.user_id
    """


def _serialize_link(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    data = dict(row)
    timezone_name = str(data.pop("user_timezone", db.DEFAULT_USER_TIMEZONE))
    external_key = data.pop("user_external_key", None)
    if external_key is not None:
        data["user_id"] = external_key
    for field in ("confirmed_at", "created_at", "occurred_at"):
        data[field] = _display_timestamp(data.get(field), timezone_name)
    return data


def create_task_transaction_link(
    user_id: str,
    task_id: int,
    transaction_id: int,
    *,
    basis: str = "user_confirmed",
    confirmed_at: datetime | str | None = None,
) -> dict[str, Any]:
    basis = str(basis).strip().lower()
    if basis not in LINK_BASES:
        raise ValueError("basis must be explicit or user_confirmed")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            raise TaskTransactionLinkNotFoundError("link objects are not owned by the current user")
        timezone_name = _timezone(connection, user_pk)
        task = connection.execute(
            "SELECT 1 FROM tasks WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (int(task_id), user_pk),
        ).fetchone()
        transaction = connection.execute(
            "SELECT 1 FROM transactions WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (int(transaction_id), user_pk),
        ).fetchone()
        if task is None or transaction is None:
            raise TaskTransactionLinkNotFoundError("task or transaction was not found")
        confirmed_text = (
            _utc_timestamp(
                confirmed_at,
                timezone_name=timezone_name,
                field_name="confirmed_at",
            )
            if confirmed_at is not None
            else (
                db._now_utc_timestamp(timezone_name)
                if basis == "user_confirmed"
                else None
            )
        )
        cursor = connection.execute(
            """
            INSERT INTO task_transaction_links(
                task_id, transaction_id, user_id, basis, confirmed_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (int(task_id), int(transaction_id), user_pk, basis, confirmed_text),
        )
        connection.commit()
        row = connection.execute(
            _link_select() + " WHERE links.id = ? AND links.user_id = ?",
            (int(cursor.lastrowid), user_pk),
        ).fetchone()
        result = _serialize_link(row)
        if result is None:
            raise RuntimeError("created task link could not be loaded")
        return result
    except sqlite3.IntegrityError as error:
        connection.rollback()
        raise ValueError("task and transaction are already linked") from error
    finally:
        connection.close()


def get_task_transaction_link(user_id: str, link_id: int) -> dict[str, Any] | None:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return None
        return _serialize_link(
            connection.execute(
                _link_select() + " WHERE links.id = ? AND links.user_id = ?",
                (int(link_id), user_pk),
            ).fetchone()
        )
    finally:
        connection.close()


def list_task_transaction_links(
    user_id: str,
    *,
    task_id: int | None = None,
    transaction_id: int | None = None,
) -> list[dict[str, Any]]:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return []
        sql = _link_select() + " WHERE links.user_id = ?"
        params: list[Any] = [user_pk]
        if task_id is not None:
            sql += " AND links.task_id = ?"
            params.append(int(task_id))
        if transaction_id is not None:
            sql += " AND links.transaction_id = ?"
            params.append(int(transaction_id))
        sql += " ORDER BY links.created_at DESC, links.id DESC"
        return [
            item
            for row in connection.execute(sql, params).fetchall()
            if (item := _serialize_link(row)) is not None
        ]
    finally:
        connection.close()


def delete_task_transaction_link(user_id: str, link_id: int) -> bool:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        cursor = connection.execute(
            "DELETE FROM task_transaction_links WHERE id = ? AND user_id = ?",
            (int(link_id), user_pk),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


remove_task_transaction_link = delete_task_transaction_link


def _date_value(value: date | datetime | str, *, field_name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO date") from error


def _period_bounds(
    connection: sqlite3.Connection,
    user_pk: int,
    *,
    period: str,
    range_start: date | datetime | str | None,
    range_end: date | datetime | str | None,
    reference_at: datetime | str | None,
) -> tuple[str, str, str, str]:
    timezone_name = _timezone(connection, user_pk)
    period = str(period or "custom").strip().lower()
    if period not in {"day", "week", "month", "year", "custom"}:
        raise ValueError("period must be day, week, month, year, or custom")
    if period == "custom":
        if range_start is None or range_end is None:
            raise ValueError("custom statistics require range_start and range_end")
        start_text = str(range_start).strip()
        end_text = str(range_end).strip()
        start_is_date = len(start_text) == 10 and "T" not in start_text
        end_is_date = len(end_text) == 10 and "T" not in end_text
        if start_is_date:
            start_date = _date_value(range_start, field_name="range_start")
            start_local = datetime.combine(start_date, time.min)
        else:
            start_local = db._parse_timestamp(
                start_text, timezone_name=timezone_name, field_name="range_start"
            ).astimezone(ZoneInfo(timezone_name))
        if end_is_date:
            end_date = _date_value(range_end, field_name="range_end")
            end_local = datetime.combine(end_date, time.min)
        else:
            end_local = db._parse_timestamp(
                end_text, timezone_name=timezone_name, field_name="range_end"
            ).astimezone(ZoneInfo(timezone_name))
    else:
        if reference_at is None:
            reference_local = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
        else:
            reference_local = db._parse_timestamp(
                reference_at,
                timezone_name=timezone_name,
                field_name="reference_at",
            ).astimezone(ZoneInfo(timezone_name))
        current = reference_local.date()
        if period == "day":
            start_date = current
            end_date = current + timedelta(days=1)
        elif period == "week":
            start_date = current - timedelta(days=current.weekday())
            end_date = start_date + timedelta(days=7)
        elif period == "month":
            start_date = current.replace(day=1)
            end_date = (
                date(start_date.year + 1, 1, 1)
                if start_date.month == 12
                else date(start_date.year, start_date.month + 1, 1)
            )
        else:
            start_date = date(current.year, 1, 1)
            end_date = date(current.year + 1, 1, 1)
        start_local = datetime.combine(start_date, time.min)
        end_local = datetime.combine(end_date, time.min)
    if end_local <= start_local:
        raise ValueError("range_end must be after range_start")
    zone = ZoneInfo(timezone_name)
    if start_local.tzinfo is None:
        start_local = start_local.replace(tzinfo=zone)
    if end_local.tzinfo is None:
        end_local = end_local.replace(tzinfo=zone)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    return (
        db._format_utc_timestamp(start_utc),
        db._format_utc_timestamp(end_utc),
        start_local.strftime("%Y-%m-%d %H:%M:%S"),
        end_local.strftime("%Y-%m-%d %H:%M:%S"),
    )


def query_statistics(
    user_id: str,
    *,
    period: str = "custom",
    range_start: date | datetime | str | None = None,
    range_end: date | datetime | str | None = None,
    reference_at: datetime | str | None = None,
    task_id: int | None = None,
    status: str | None = None,
    transaction_type: str | None = None,
    category: str | None = None,
    currency: str | None = None,
    session_id: int | None = None,
    persist_query: bool = True,
) -> dict[str, Any]:
    """Return database-backed task and finance metrics for one user."""

    if transaction_type is not None:
        transaction_type = str(transaction_type).strip().lower()
        if transaction_type not in TRANSACTION_TYPES:
            raise ValueError("type must be expense or income")
    if status is not None:
        status = str(status).strip().lower()
        if status not in db.TASK_STATUSES:
            raise ValueError("status must be a valid task status")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        connection.commit()
        timezone_name = _timezone(connection, user_pk)
        start_utc, end_utc, local_start, local_end = _period_bounds(
            connection,
            user_pk,
            period=period,
            range_start=range_start,
            range_end=range_end,
            reference_at=reference_at,
        )
        occurrence_where = [
            "task_occurrences.user_id = ?",
            "task_occurrences.occurred_at >= ?",
            "task_occurrences.occurred_at < ?",
            "task_occurrences.deleted_at IS NULL",
        ]
        occurrence_params: list[Any] = [user_pk, start_utc, end_utc]
        if task_id is not None:
            occurrence_where.append("task_id = ?")
            occurrence_params.append(int(task_id))
        occurrence_sql = " AND ".join(occurrence_where)
        occurrence_rows = connection.execute(
            f"""
            SELECT task_occurrences.status, task_occurrences.task_id,
                   task_occurrences.occurred_at, tasks.status AS task_status
            FROM task_occurrences
            JOIN tasks ON tasks.id = task_occurrences.task_id
                       AND tasks.user_id = task_occurrences.user_id
            WHERE {occurrence_sql}
            """,
            occurrence_params,
        ).fetchall()
        if status is not None:
            occurrence_rows = [
                row
                for row in occurrence_rows
                if row["task_status"] == status
            ]
        valid_rows = [row for row in occurrence_rows if row["status"] in {"started", "completed"}]
        completed_count = sum(row["status"] == "completed" for row in valid_rows)
        by_task: dict[str, int] = {}
        by_day: dict[str, int] = {}
        for row in valid_rows:
            task_key = str(row["task_id"])
            by_task[task_key] = by_task.get(task_key, 0) + 1
            local_day = datetime.fromisoformat(
                str(row["occurred_at"]).replace("Z", "+00:00")
            ).astimezone(ZoneInfo(timezone_name)).date().isoformat()
            by_day[local_day] = by_day.get(local_day, 0) + 1
        eligible_count = len(valid_rows)
        finance_where = [
            "user_id = ?",
            "occurred_at >= ?",
            "occurred_at < ?",
            "deleted_at IS NULL",
        ]
        finance_params: list[Any] = [user_pk, start_utc, end_utc]
        if transaction_type is not None:
            finance_where.append("type = ?")
            finance_params.append(transaction_type)
        if category is not None:
            finance_where.append("category = ?")
            finance_params.append(_required_text(category, "category", max_length=100))
        if currency is not None:
            finance_where.append("currency = ?")
            finance_params.append(_currency(currency, connection, user_pk))
        finance_sql = " AND ".join(finance_where)
        transaction_rows = connection.execute(
            f"SELECT type, amount_minor, currency, category FROM transactions WHERE {finance_sql}",
            finance_params,
        ).fetchall()
        income_total: dict[str, int] = {}
        expense_total: dict[str, int] = {}
        category_totals: dict[tuple[str, str, str], dict[str, int]] = {}
        for row in transaction_rows:
            target = income_total if row["type"] == "income" else expense_total
            target[row["currency"]] = target.get(row["currency"], 0) + int(row["amount_minor"])
            key = (row["type"], row["category"], row["currency"])
            item = category_totals.setdefault(key, {"total_minor": 0, "count": 0})
            item["total_minor"] += int(row["amount_minor"])
            item["count"] += 1
        by_category = [
            {
                "type": transaction_type_value,
                "category": category_value,
                "currency": currency_value,
                **totals,
            }
            for (transaction_type_value, category_value, currency_value), totals
            in sorted(category_totals.items())
        ]
        currencies = sorted({row["currency"] for row in transaction_rows})
        local_start_date = date.fromisoformat(local_start[:10])
        local_end_date = date.fromisoformat(local_end[:10])
        goal_rows = connection.execute(
            """
            SELECT id, task_id, period_start, period_end, target_count,
                   completed_count, status, completed_at
            FROM goal_progress
            WHERE user_id = ?
              AND period_start < ?
              AND period_end >= ?
            ORDER BY period_start ASC, id ASC
            """,
            [user_pk, local_end_date.isoformat(), local_start_date.isoformat()],
        ).fetchall()
        now_local_date = datetime.now(timezone.utc).astimezone(
            ZoneInfo(timezone_name)
        ).date()
        goal_progress = []
        for row in goal_rows:
            goal_start = _date_value(row["period_start"], field_name="period_start")
            goal_end = _date_value(row["period_end"], field_name="period_end")
            query_start = max(goal_start, local_start_date)
            query_end = min(goal_end + timedelta(days=1), local_end_date)
            if query_end <= query_start:
                continue
            goal_start_utc = _utc_timestamp(
                f"{query_start.isoformat()}T00:00:00",
                timezone_name=timezone_name,
                field_name="period_start",
            )
            goal_end_utc = _utc_timestamp(
                f"{query_end.isoformat()}T00:00:00",
                timezone_name=timezone_name,
                field_name="period_end",
            )
            completed = connection.execute(
                """
                SELECT COUNT(*) FROM task_occurrences
                WHERE user_id = ? AND task_id = ? AND status = 'completed'
                  AND deleted_at IS NULL AND occurred_at >= ? AND occurred_at < ?
                """,
                [user_pk, row["task_id"], goal_start_utc, goal_end_utc],
            ).fetchone()[0]
            completed = int(completed)
            if completed >= int(row["target_count"]):
                goal_status = "completed"
            elif now_local_date > goal_end:
                goal_status = "expired"
            else:
                goal_status = "active"
            goal_progress.append(
                {
                    "id": int(row["id"]),
                    "task_id": int(row["task_id"]),
                    "period_start": row["period_start"],
                    "period_end": row["period_end"],
                    "target_count": int(row["target_count"]),
                    "completed_count": completed,
                    "progress_ratio": min(
                        1.0,
                        completed / int(row["target_count"]),
                    ),
                    "status": goal_status,
                    "completed_at": row["completed_at"],
                }
            )
        series = []
        local_cursor = local_start_date
        while local_cursor < local_end_date:
            day_key = local_cursor.isoformat()
            series.append({"period": day_key, "count": by_day.get(day_key, 0)})
            local_cursor += timedelta(days=1)
        result = {
            "query_ok": True,
            "timezone": timezone_name,
            "range_start": local_start,
            "range_end": local_end,
            "utc_range_start": start_utc,
            "utc_range_end": end_utc,
            "query_path": "database.query_statistics",
            "task_executions": {
                "total_count": len(valid_rows),
                "by_task": [
                    {"task_id": int(key), "count": value}
                    for key, value in sorted(by_task.items(), key=lambda item: int(item[0]))
                ],
                "by_day": series,
            },
            "completion": {
                "completed_count": completed_count,
                "eligible_count": eligible_count,
                "completion_rate": (
                    completed_count / eligible_count if eligible_count else 0
                ),
            },
            "goal_progress": goal_progress,
            "finance": {
                "income_total": income_total,
                "expense_total": expense_total,
                "by_category": by_category,
                "currency": currencies,
                "transaction_count": len(transaction_rows),
            },
        }
        if persist_query:
            query_definition = {
                "period": period,
                "range_start": None if range_start is None else str(range_start),
                "range_end": None if range_end is None else str(range_end),
                "reference_at": None if reference_at is None else str(reference_at),
                "task_id": task_id,
                "status": status,
                "transaction_type": transaction_type,
                "category": category,
                "currency": currency,
            }
            from database.charts import record_chart_query

            result.update(
                record_chart_query(
                    connection,
                    user_pk,
                    query=query_definition,
                    range_start=local_start,
                    range_end=local_end,
                    timezone_name=timezone_name,
                    session_id=session_id,
                )
            )
            connection.commit()
        return result
    finally:
        connection.close()


statistics_query = query_statistics


def _upsert_agent_session_on_connection(
    connection: sqlite3.Connection,
    user_id: str,
    user_pk: int,
    session_key: str,
    *,
    request_id: str | None = None,
    expires_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Upsert a session without opening a second connection."""

    timezone_name = _timezone(connection, user_pk)
    expires_text = (
        _utc_timestamp(expires_at, timezone_name=timezone_name, field_name="expires_at")
        if expires_at is not None
        else None
    )
    connection.execute(
        """
        INSERT INTO agent_sessions(user_id, session_key, last_request_id, expires_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, session_key) DO UPDATE SET
            status = 'active',
            last_request_id = COALESCE(excluded.last_request_id, agent_sessions.last_request_id),
            expires_at = COALESCE(excluded.expires_at, agent_sessions.expires_at),
            updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
        """,
        (user_pk, session_key, request_id, expires_text),
    )
    row = connection.execute(
        "SELECT * FROM agent_sessions WHERE user_id = ? AND session_key = ?",
        (user_pk, session_key),
    ).fetchone()
    data = dict(row)
    for field in ("created_at", "updated_at", "expires_at"):
        data[field] = _display_timestamp(data.get(field), timezone_name)
    data["user_id"] = user_id
    return data


def upsert_agent_session(
    user_id: str,
    session_key: str,
    *,
    request_id: str | None = None,
    expires_at: datetime | str | None = None,
) -> dict[str, Any]:
    session_key = _required_text(session_key, "session_key", max_length=200)
    if request_id is not None:
        request_id = _required_text(request_id, "request_id", max_length=200)
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        data = _upsert_agent_session_on_connection(
            connection,
            user_id,
            user_pk,
            session_key,
            request_id=request_id,
            expires_at=expires_at,
        )
        connection.commit()
        return data
    finally:
        connection.close()


def get_agent_session(user_id: str, session_key: str) -> dict[str, Any] | None:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return None
        row = connection.execute(
            "SELECT * FROM agent_sessions WHERE user_id = ? AND session_key = ?",
            (user_pk, str(session_key).strip()),
        ).fetchone()
        if row is None:
            return None
        timezone_name = _timezone(connection, user_pk)
        data = dict(row)
        for field in ("created_at", "updated_at", "expires_at"):
            data[field] = _display_timestamp(data.get(field), timezone_name)
        data["user_id"] = user_id
        return data
    finally:
        connection.close()


def archive_agent_session(user_id: str, session_key: str) -> bool:
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return False
        cursor = connection.execute(
            """
            UPDATE agent_sessions
            SET status = 'archived', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE user_id = ? AND session_key = ? AND status = 'active'
            """,
            (user_pk, str(session_key).strip()),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


def record_tool_audit(
    user_id: str,
    *,
    request_id: str,
    tool_name: str,
    permission_level: str = "read",
    tool_call_id: str | None = None,
    session_key: str | None = None,
    input_digest: str | None = None,
    args_summary: dict[str, Any] | None = None,
    success: bool = False,
    status: str | None = None,
    duration_ms: int | None = None,
    error_code: str | None = None,
) -> int | None:
    """Write a bounded audit row; raw tool arguments are never persisted."""

    if not str(request_id).strip() or not str(tool_name).strip():
        raise ValueError("request_id and tool_name must not be blank")
    if permission_level not in {"read", "write", "high_risk"}:
        raise ValueError("invalid permission level")
    if duration_ms is not None and (type(duration_ms) is not int or duration_ms < 0):
        raise ValueError("duration_ms must be a non-negative integer")
    if status is None:
        status = "success" if success else "failed"
    if status not in {"success", "failed", "denied", "timeout"}:
        raise ValueError("invalid audit status")
    if (success and status != "success") or (not success and status == "success"):
        raise ValueError("audit success and status disagree")
    summary = None
    if args_summary is not None:
        summary = json.dumps(
            {
                str(key): type(value).__name__
                for key, value in list(args_summary.items())[:16]
            },
            sort_keys=True,
            ensure_ascii=False,
        )[:2000]
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=True)
        assert user_pk is not None
        session_id = None
        if session_key:
            session_key = _required_text(session_key, "session_key", max_length=200)
            session = _upsert_agent_session_on_connection(
                connection,
                user_id,
                user_pk,
                session_key,
                request_id=request_id,
            )
            session_id = int(session["id"])
        cursor = connection.execute(
            """
            INSERT INTO tool_audit_logs(
                user_id, session_id, session_key, request_id, tool_call_id,
                tool_name, permission_level, args_summary, input_digest,
                success, status, duration_ms, error_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_pk,
                session_id,
                session_key,
                str(request_id),
                tool_call_id,
                str(tool_name),
                permission_level,
                summary,
                input_digest,
                int(bool(success)),
                status,
                duration_ms,
                error_code,
            ),
        )
        connection.commit()
        return int(cursor.lastrowid)
    except sqlite3.OperationalError:
        # Keep the generic M1-M3 dispatcher usable before the M4 migration.
        connection.rollback()
        return None
    finally:
        connection.close()


def list_tool_audits(
    user_id: str,
    *,
    request_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    if not 1 <= limit <= 500:
        raise ValueError("limit must be 1..500")
    connection = db.get_connection()
    try:
        user_pk = _user_pk(connection, user_id, create=False)
        if user_pk is None:
            return []
        sql = "SELECT * FROM tool_audit_logs WHERE user_id = ?"
        params: list[Any] = [user_pk]
        if request_id is not None:
            sql += " AND request_id = ?"
            params.append(request_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in connection.execute(sql, params).fetchall()]
    finally:
        connection.close()


__all__ = [
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
]
