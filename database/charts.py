"""M10 statistics datasets and allow-listed chart artifacts."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

from database import db


CHART_TYPES = frozenset({"line", "bar", "pie", "table"})
CHART_DIMENSIONS = frozenset(
    {
        "period",
        "task_id",
        "id",
        "type",
        "category",
        "currency",
        "period_start",
        "period_end",
        "status",
    }
)
CHART_METRICS = frozenset(
    {
        "count",
        "total_minor",
        "completed_count",
        "target_count",
        "progress_ratio",
    }
)


class ChartNotFoundError(LookupError):
    """A chart dataset is absent or belongs to another user."""


class ChartSpecError(ValueError):
    """A requested chart cannot be represented by the fixed ChartSpec schema."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _safe_text(value: Any, field: str, maximum: int) -> str:
    text = str(value or "").strip()
    if not text or len(text) > maximum or any(char in text for char in "<>\x00"):
        raise ChartSpecError(f"{field} is invalid")
    return text


def record_chart_query(
    connection: sqlite3.Connection,
    user_pk: int,
    *,
    query: dict[str, Any],
    range_start: str,
    range_end: str,
    timezone_name: str,
    session_id: int | None = None,
    ttl_seconds: int = 86400,
) -> dict[str, Any]:
    """Persist a validated statistics definition and return its public ref."""

    if not isinstance(query, dict):
        raise ChartSpecError("statistics query must be an object")
    if ttl_seconds < 60:
        raise ChartSpecError("chart query ttl must be at least 60 seconds")
    if session_id is not None:
        owned = connection.execute(
            "SELECT 1 FROM agent_sessions WHERE id = ? AND user_id = ?",
            (session_id, user_pk),
        ).fetchone()
        if owned is None:
            raise ChartSpecError("session does not belong to the current user")
    digest = _digest(query)
    expires_at = _utc_text(datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds))
    cursor = connection.execute(
        """
        INSERT INTO chart_queries(
            user_id, session_id, query_json, range_start, range_end,
            timezone, query_digest, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_pk,
            session_id,
            _canonical(query),
            str(range_start),
            str(range_end),
            str(timezone_name),
            digest,
            expires_at,
        ),
    )
    query_id = int(cursor.lastrowid)
    return {
        "dataset_ref": f"statistics:{query_id}",
        "query_digest": digest,
        "query_id": query_id,
        "expires_at": expires_at,
    }


def _query_row(
    connection: sqlite3.Connection,
    user_pk: int,
    dataset_ref: str,
) -> sqlite3.Row:
    if not isinstance(dataset_ref, str) or not dataset_ref.startswith("statistics:"):
        raise ChartSpecError("dataset_ref must be a statistics dataset")
    try:
        query_id = int(dataset_ref.split(":", 1)[1])
    except (IndexError, TypeError, ValueError) as error:
        raise ChartSpecError("dataset_ref is invalid") from error
    if query_id <= 0:
        raise ChartSpecError("dataset_ref is invalid")
    row = connection.execute(
        "SELECT * FROM chart_queries WHERE id = ? AND user_id = ?",
        (query_id, user_pk),
    ).fetchone()
    if row is None:
        raise ChartNotFoundError("statistics dataset was not found")
    if row["expires_at"] and str(row["expires_at"]) <= _utc_text(datetime.now(timezone.utc)):
        raise ChartNotFoundError("statistics dataset has expired")
    return row


def _query_kwargs(query: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "period",
        "range_start",
        "range_end",
        "reference_at",
        "task_id",
        "status",
        "transaction_type",
        "category",
        "currency",
    }
    return {key: value for key, value in query.items() if key in allowed}


def _dataset_rows(
    statistics: dict[str, Any],
    dimensions: tuple[str, ...],
    metric: str,
) -> list[dict[str, Any]]:
    if dimensions == ("period",):
        rows = statistics["task_executions"]["by_day"]
    elif dimensions == ("task_id",):
        rows = statistics["task_executions"]["by_task"]
    elif "category" in dimensions:
        rows = statistics["finance"]["by_category"]
    elif "period_start" in dimensions or "progress_ratio" == metric:
        rows = statistics["goal_progress"]
    else:
        raise ChartSpecError("dimensions do not identify an allow-listed dataset")

    result: list[dict[str, Any]] = []
    for row in rows:
        if any(field not in row for field in dimensions) or metric not in row:
            raise ChartSpecError("dimension or metric is unavailable in the dataset")
        result.append({field: row[field] for field in (*dimensions, metric)})
    return result


def build_chart(
    user_id: str,
    *,
    chart_type: str,
    dataset_ref: str,
    dimensions: list[str],
    metric: str,
    query_digest: str | None = None,
    title: str | None = None,
    ttl_seconds: int = 86400,
) -> dict[str, Any]:
    """Build or return a cached, user-owned fixed ChartSpec artifact."""

    chart_type = str(chart_type).strip().lower()
    if chart_type not in CHART_TYPES:
        raise ChartSpecError("chart_type must be line, bar, pie, or table")
    if not isinstance(dimensions, list) or not 1 <= len(dimensions) <= 3:
        raise ChartSpecError("dimensions must contain 1..3 items")
    normalized_dimensions = tuple(str(item).strip() for item in dimensions)
    if (
        len(set(normalized_dimensions)) != len(normalized_dimensions)
        or any(item not in CHART_DIMENSIONS for item in normalized_dimensions)
    ):
        raise ChartSpecError("dimensions contain an unsupported field")
    metric = str(metric).strip()
    if metric not in CHART_METRICS:
        raise ChartSpecError("metric is not supported")
    if chart_type == "pie" and len(normalized_dimensions) != 1:
        raise ChartSpecError("pie charts require exactly one dimension")
    if ttl_seconds < 60:
        raise ChartSpecError("chart ttl must be at least 60 seconds")
    title = _safe_text(title, "title", 200) if title is not None else None

    connection = db.get_connection()
    try:
        user_pk = connection.execute(
            "SELECT id FROM users WHERE external_key = ? AND status = 'active'",
            (db._validate_user_id(user_id),),
        ).fetchone()
        if user_pk is None:
            raise ChartNotFoundError("statistics dataset was not found")
        user_pk_value = int(user_pk["id"])
        row = _query_row(connection, user_pk_value, dataset_ref)
        stored_digest = str(row["query_digest"])
        if query_digest is not None and not hmac.compare_digest(str(query_digest), stored_digest):
            raise ChartSpecError("query_digest does not match dataset_ref")
        try:
            query = json.loads(row["query_json"])
        except (TypeError, json.JSONDecodeError) as error:
            raise ChartSpecError("stored statistics query is invalid") from error

        from database.m4 import query_statistics

        statistics = query_statistics(
            user_id,
            persist_query=False,
            **_query_kwargs(query),
        )
        data = _dataset_rows(statistics, normalized_dimensions, metric)
        spec = {
            "version": 1,
            "chart_type": chart_type,
            "dataset_ref": dataset_ref,
            "query_digest": stored_digest,
            "title": title or f"{metric} by {', '.join(normalized_dimensions)}",
            "dimensions": list(normalized_dimensions),
            "metric": metric,
            "timezone": row["timezone"],
            "range_start": row["range_start"],
            "range_end": row["range_end"],
            "data": data,
        }
        spec_digest = _digest({
            "chart_type": chart_type,
            "dimensions": normalized_dimensions,
            "metric": metric,
            "title": spec["title"],
            "data": data,
        })
        data_digest = _digest(data)
        now = _utc_text(datetime.now(timezone.utc))
        cached = connection.execute(
            """
            SELECT * FROM chart_artifacts
            WHERE query_id = ? AND spec_digest = ? AND expires_at > ?
            """,
            (row["id"], spec_digest, now),
        ).fetchone()
        if cached is not None:
            return {
                "artifact_id": int(cached["id"]),
                "dataset_ref": dataset_ref,
                "query_digest": stored_digest,
                "spec": json.loads(cached["spec_json"]),
                "data_digest": cached["data_digest"],
                "expires_at": cached["expires_at"],
                "cached": True,
            }

        expires_at = _utc_text(datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds))
        existing = connection.execute(
            "SELECT id FROM chart_artifacts WHERE query_id = ? AND spec_digest = ?",
            (row["id"], spec_digest),
        ).fetchone()
        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO chart_artifacts(
                    query_id, chart_type, spec_json, data_digest, spec_digest, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (row["id"], chart_type, _canonical(spec), data_digest, spec_digest, expires_at),
            )
            artifact_id = int(cursor.lastrowid)
        else:
            artifact_id = int(existing["id"])
            connection.execute(
                """
                UPDATE chart_artifacts
                SET chart_type = ?, spec_json = ?, data_digest = ?, expires_at = ?,
                    created_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ?
                """,
                (chart_type, _canonical(spec), data_digest, expires_at, artifact_id),
            )
        connection.commit()
        return {
            "artifact_id": artifact_id,
            "dataset_ref": dataset_ref,
            "query_digest": stored_digest,
            "spec": spec,
            "data_digest": data_digest,
            "expires_at": expires_at,
            "cached": False,
        }
    finally:
        connection.close()


__all__ = [
    "CHART_DIMENSIONS",
    "CHART_METRICS",
    "CHART_TYPES",
    "ChartNotFoundError",
    "ChartSpecError",
    "build_chart",
    "record_chart_query",
]
