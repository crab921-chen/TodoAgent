"""M9 plan draft, confirmation, and atomic application services."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database import db


class PlanNotFoundError(LookupError):
    """A plan or draft is absent or belongs to another user."""


class PlanDraftError(ValueError):
    """A plan draft cannot be confirmed or applied."""


PLAN_STATUSES = frozenset({"draft", "confirmed", "active", "completed", "archived"})
DRAFT_STATUSES = frozenset({"draft", "applied", "expired", "rejected"})
DEPENDENCY_TYPES = frozenset(
    {"finish_to_start", "start_to_start", "finish_to_finish", "start_to_finish"}
)
MAX_PHASES = 100
MAX_TASKS = 500


def _text(value: Any, field: str, maximum: int) -> str:
    result = str(value or "").strip()
    if not result:
        raise PlanDraftError(f"{field} must not be blank")
    if len(result) > maximum:
        raise PlanDraftError(f"{field} is too long")
    return result


def _date_text(value: Any, field: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as error:
        raise PlanDraftError(f"{field} must be an ISO date") from error


def _timestamp_value(value: Any, field: str, timezone_name: str | None) -> Any:
    if value is None or str(value).strip() == "":
        return None
    try:
        db._normalize_timestamp(
            value,
            timezone_name=timezone_name or db.DEFAULT_USER_TIMEZONE,
            field_name=field,
        )
    except (TypeError, ValueError) as error:
        raise PlanDraftError(f"{field} must be an ISO-8601 timestamp") from error
    return value


def _validate_recurrence(value: Any, timezone_name: str | None) -> Any:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PlanDraftError("task.recurrence must be an object")
    frequency = str(value.get("frequency", "custom")).lower()
    if frequency not in {"daily", "weekly", "monthly", "custom"}:
        raise PlanDraftError("unsupported recurrence frequency")
    try:
        interval = int(value.get("interval_value", 1))
    except (TypeError, ValueError) as error:
        raise PlanDraftError("recurrence.interval_value must be an integer") from error
    if interval < 1:
        raise PlanDraftError("recurrence.interval_value must be positive")
    start = _date_text(value.get("start_date"), "recurrence.start_date")
    end = _date_text(value.get("end_date"), "recurrence.end_date")
    if start and end and end < start:
        raise PlanDraftError("recurrence.end_date must not be before recurrence.start_date")
    recurrence_timezone = value.get("timezone") or timezone_name
    if recurrence_timezone is not None:
        try:
            db._validate_timezone(recurrence_timezone)
        except ValueError as error:
            raise PlanDraftError("recurrence.timezone must be a valid IANA timezone") from error
    return value


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _session_pk(connection: sqlite3.Connection, user_pk: int, session_id: int | None) -> int | None:
    if session_id is None:
        return None
    row = connection.execute(
        "SELECT id FROM agent_sessions WHERE id = ? AND user_id = ?",
        (session_id, user_pk),
    ).fetchone()
    if row is None:
        raise PlanDraftError("session does not belong to the current user")
    return int(row["id"])


def _normalize_task(task: Any, phase_index: int, task_index: int) -> dict[str, Any]:
    if isinstance(task, str):
        task = {"title": task}
    if not isinstance(task, dict):
        raise PlanDraftError("each plan task must be an object")
    key = _text(task.get("key") or f"phase-{phase_index + 1}-task-{task_index + 1}", "task.key", 100)
    timezone_name = task.get("timezone")
    if timezone_name is not None:
        try:
            timezone_name = db._validate_timezone(timezone_name)
        except ValueError as error:
            raise PlanDraftError("task.timezone must be a valid IANA timezone") from error
    result = {
        "key": key,
        "title": _text(task.get("title"), "task.title", 200),
        "description": (
            None
            if task.get("description") in (None, "")
            else _text(task.get("description"), "task.description", 10000)
        ),
        "planned_start_at": _timestamp_value(task.get("planned_start_at"), "task.planned_start_at", timezone_name),
        "due_at": _timestamp_value(task.get("due_at"), "task.due_at", timezone_name),
        "timezone": timezone_name,
        "relation_type": _text(task.get("relation_type", "primary"), "task.relation_type", 50),
        "existing_task_id": task.get("existing_task_id"),
        "recurrence": _validate_recurrence(task.get("recurrence"), timezone_name),
    }
    if result["planned_start_at"] and result["due_at"]:
        planned = db._normalize_timestamp(
            result["planned_start_at"],
            timezone_name=timezone_name or db.DEFAULT_USER_TIMEZONE,
            field_name="task.planned_start_at",
        )
        due = db._normalize_timestamp(
            result["due_at"],
            timezone_name=timezone_name or db.DEFAULT_USER_TIMEZONE,
            field_name="task.due_at",
        )
        if planned is not None and due is not None and due < planned:
            raise PlanDraftError("task.due_at must not be before task.planned_start_at")
    if result["existing_task_id"] is not None:
        try:
            result["existing_task_id"] = int(result["existing_task_id"])
        except (TypeError, ValueError) as error:
            raise PlanDraftError("existing_task_id must be an integer") from error
        if result["existing_task_id"] <= 0:
            raise PlanDraftError("existing_task_id must be positive")
    return result


def _normalize_payload(
    intent: str,
    constraints: dict[str, Any] | None,
    existing_context: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    if constraints is not None and not isinstance(constraints, dict):
        raise PlanDraftError("constraints must be an object")
    if existing_context is not None and not isinstance(existing_context, dict):
        raise PlanDraftError("existing_context must be an object")
    constraints = constraints or {}
    existing_context = existing_context or {}
    goal = _text(intent, "intent", 10000)
    title = _text(constraints.get("title") or goal[:200], "title", 200)
    raw_phases = constraints.get("phases") or existing_context.get("phases")
    warnings: list[str] = []
    if not raw_phases:
        raw_phases = [{"name": "Initial phase", "tasks": [{"title": title}]}]
        warnings.append("No phases were supplied; an initial phase was generated.")
    if not isinstance(raw_phases, list) or not 1 <= len(raw_phases) <= MAX_PHASES:
        raise PlanDraftError(f"phases must contain 1..{MAX_PHASES} items")
    phases: list[dict[str, Any]] = []
    task_keys: set[str] = set()
    total_tasks = 0
    for phase_index, raw_phase in enumerate(raw_phases):
        if isinstance(raw_phase, str):
            raw_phase = {"name": raw_phase, "tasks": []}
        if not isinstance(raw_phase, dict):
            raise PlanDraftError("each plan phase must be an object")
        raw_tasks = raw_phase.get("tasks") or []
        if not isinstance(raw_tasks, list):
            raise PlanDraftError("phase.tasks must be a list")
        phase_tasks = []
        for task_index, raw_task in enumerate(raw_tasks):
            task = _normalize_task(raw_task, phase_index, task_index)
            if task["key"] in task_keys:
                raise PlanDraftError(f"duplicate task key: {task['key']}")
            task_keys.add(task["key"])
            phase_tasks.append(task)
            total_tasks += 1
        if not phase_tasks:
            phase_tasks.append(_normalize_task({"title": raw_phase.get("name", "Phase task")}, phase_index, 0))
            if phase_tasks[0]["key"] in task_keys:
                raise PlanDraftError(f"duplicate task key: {phase_tasks[0]['key']}")
            task_keys.add(phase_tasks[0]["key"])
            total_tasks += 1
            warnings.append(f"Phase {phase_index + 1} had no tasks; a task was generated.")
        start_date = _date_text(raw_phase.get("start_date"), "phase.start_date")
        end_date = _date_text(raw_phase.get("end_date"), "phase.end_date")
        if start_date and end_date and end_date < start_date:
            raise PlanDraftError("phase.end_date must not be before phase.start_date")
        phases.append({
            "name": _text(raw_phase.get("name") or f"Phase {phase_index + 1}", "phase.name", 200),
            "start_date": start_date,
            "end_date": end_date,
            "tasks": phase_tasks,
        })
    if total_tasks > MAX_TASKS:
        raise PlanDraftError(f"tasks must contain at most {MAX_TASKS} items")
    dependencies = constraints.get("dependencies") or existing_context.get("dependencies") or []
    if not isinstance(dependencies, list):
        raise PlanDraftError("dependencies must be a list")
    normalized_dependencies = []
    dependency_keys: set[tuple[str, str]] = set()
    for dependency in dependencies:
        if not isinstance(dependency, dict):
            raise PlanDraftError("each dependency must be an object")
        predecessor = _text(dependency.get("predecessor"), "dependency.predecessor", 100)
        successor = _text(dependency.get("successor"), "dependency.successor", 100)
        dependency_key = (predecessor, successor)
        if dependency_key in dependency_keys:
            raise PlanDraftError("duplicate plan dependency")
        dependency_keys.add(dependency_key)
        dependency_type = _text(
            dependency.get("dependency_type", "finish_to_start"),
            "dependency_type",
            50,
        )
        if dependency_type not in DEPENDENCY_TYPES:
            raise PlanDraftError("unsupported dependency_type")
        normalized_dependencies.append({
            "predecessor": predecessor,
            "successor": successor,
            "dependency_type": dependency_type,
        })
    base_plan_id = constraints.get("base_plan_id")
    if base_plan_id is not None:
        try:
            base_plan_id = int(base_plan_id)
        except (TypeError, ValueError) as error:
            raise PlanDraftError("base_plan_id must be an integer") from error
        if base_plan_id <= 0:
            raise PlanDraftError("base_plan_id must be positive")
    milestones = constraints.get("milestones", [])
    if not isinstance(milestones, list) or len(milestones) > MAX_PHASES:
        raise PlanDraftError(f"milestones must be a list with at most {MAX_PHASES} items")
    payload = {
        "title": title,
        "goal": goal,
        "base_plan_id": base_plan_id,
        "milestones": milestones,
        "phases": phases,
        "dependencies": normalized_dependencies,
    }
    return payload, warnings


def _validate_dag(payload: dict[str, Any]) -> None:
    keys = {
        task["key"]
        for phase in payload["phases"]
        for task in phase["tasks"]
    }
    graph = {key: set() for key in keys}
    for dependency in payload["dependencies"]:
        predecessor = dependency["predecessor"]
        successor = dependency["successor"]
        if predecessor not in keys or successor not in keys:
            raise PlanDraftError("dependency references an unknown task key")
        if predecessor == successor:
            raise PlanDraftError("a task cannot depend on itself")
        graph[predecessor].add(successor)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key in visiting:
            raise PlanDraftError("plan dependencies must form an acyclic graph")
        if key in visited:
            return
        visiting.add(key)
        for child in graph[key]:
            visit(child)
        visiting.remove(key)
        visited.add(key)

    for key in graph:
        visit(key)


def _serialize_draft(row: sqlite3.Row, payload: dict[str, Any], warnings: list[str], token: str | None) -> dict[str, Any]:
    return {
        "draft_id": int(row["id"]),
        "status": row["status"],
        "request_id": row["request_id"],
        "title": payload["title"],
        "goal": payload["goal"],
        "phases": payload["phases"],
        "dependencies": payload["dependencies"],
        "milestones": payload.get("milestones", []),
        "warnings": warnings,
        "confirmation_token": token,
        "expires_at": row["expires_at"],
    }


def create_plan_draft(
    user_id: str,
    *,
    intent: str,
    constraints: dict[str, Any] | None = None,
    existing_context: dict[str, Any] | None = None,
    request_id: str,
    session_id: int | None = None,
    ttl_seconds: int = 86400,
) -> dict[str, Any]:
    if ttl_seconds < 60:
        raise ValueError("ttl_seconds must be at least 60")
    payload, warnings = _normalize_payload(intent, constraints, existing_context)
    _validate_dag(payload)
    connection = db.get_connection()
    try:
        user_pk = db._ensure_user(connection, db._validate_user_id(user_id))
        owned_session = _session_pk(connection, user_pk, session_id)
        request_id = _text(request_id, "request_id", 200)
        existing = connection.execute(
            "SELECT * FROM plan_drafts WHERE user_id = ? AND request_id = ?",
            (user_pk, request_id),
        ).fetchone()
        token = secrets.token_urlsafe(32)
        expires_at = _utc_text(_utc_now() + timedelta(seconds=ttl_seconds))
        payload_text = _canonical(payload)
        warnings_text = _canonical(warnings)
        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO plan_drafts(
                    user_id, session_id, request_id, payload_json, warnings_json,
                    confirmation_digest, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_pk, owned_session, request_id, payload_text, warnings_text, _token_digest(token), expires_at),
            )
            draft_id = int(cursor.lastrowid)
        elif existing["status"] == "applied":
            payload = json.loads(existing["payload_json"])
            warnings = json.loads(existing["warnings_json"])
            connection.commit()
            return _serialize_draft(existing, payload, warnings, None)
        else:
            connection.execute(
                """
                UPDATE plan_drafts
                SET session_id = ?, payload_json = ?, warnings_json = ?, status = 'draft',
                    confirmation_digest = ?, expires_at = ?, applied_plan_id = NULL,
                    updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
                WHERE id = ? AND user_id = ?
                """,
                (owned_session, payload_text, warnings_text, _token_digest(token), expires_at, existing["id"], user_pk),
            )
            draft_id = int(existing["id"])
        connection.commit()
        row = connection.execute("SELECT * FROM plan_drafts WHERE id = ?", (draft_id,)).fetchone()
        assert row is not None
        return _serialize_draft(row, payload, warnings, token)
    finally:
        connection.close()


def _plan_row(connection: sqlite3.Connection, user_pk: int, plan_id: int) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM plans WHERE id = ? AND user_id = ?", (plan_id, user_pk)
    ).fetchone()


def get_plan(user_id: str, plan_id: int) -> dict[str, Any] | None:
    connection = db.get_connection()
    try:
        user_pk = db._ensure_user(connection, db._validate_user_id(user_id))
        plan = _plan_row(connection, user_pk, plan_id)
        if plan is None:
            return None
        phases = []
        for phase in connection.execute(
            "SELECT * FROM plan_phases WHERE plan_id = ? ORDER BY sequence_no", (plan_id,)
        ):
            phase_data = dict(phase)
            phase_data["tasks"] = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT plan_tasks.*, tasks.title, tasks.description, tasks.status,
                           tasks.planned_start_at, tasks.due_at, tasks.source
                    FROM plan_tasks JOIN tasks ON tasks.id = plan_tasks.task_id
                    WHERE plan_tasks.phase_id = ? ORDER BY plan_tasks.sequence_no
                    """,
                    (phase["id"],),
                )
            ]
            phases.append(phase_data)
        dependencies = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM plan_dependencies WHERE plan_id = ? ORDER BY id", (plan_id,)
            )
        ]
        result = dict(plan)
        result["phases"] = phases
        result["dependencies"] = dependencies
        result["milestones"] = json.loads(result.pop("milestones_json", "[]") or "[]")
        return result
    finally:
        connection.close()


def refresh_plan_progress_for_task(
    connection: sqlite3.Connection,
    user_pk: int,
    task_id: int,
) -> None:
    """Recompute owned plan and phase status from task facts.

    Task occurrences remain the execution history. Plan status is a derived
    view and therefore never rewrites an occurrence or a task's history.
    """

    plans = connection.execute(
        """
        SELECT DISTINCT plans.id
        FROM plans
        JOIN plan_tasks ON plan_tasks.plan_id = plans.id
        WHERE plans.user_id = ? AND plan_tasks.task_id = ?
          AND plans.status <> 'archived'
        """,
        (user_pk, task_id),
    ).fetchall()
    for plan_row in plans:
        plan_id = int(plan_row["id"])
        phases = connection.execute(
            """
            SELECT plan_phases.id,
                   COUNT(plan_tasks.id) AS task_count,
                   SUM(CASE WHEN tasks.status = 'completed' THEN 1 ELSE 0 END) AS completed_count,
                   SUM(CASE WHEN tasks.status = 'in_progress' THEN 1 ELSE 0 END) AS active_count
            FROM plan_phases
            LEFT JOIN plan_tasks ON plan_tasks.phase_id = plan_phases.id
            LEFT JOIN tasks ON tasks.id = plan_tasks.task_id
            WHERE plan_phases.plan_id = ?
            GROUP BY plan_phases.id
            ORDER BY plan_phases.sequence_no
            """,
            (plan_id,),
        ).fetchall()
        all_completed = bool(phases)
        for phase in phases:
            task_count = int(phase["task_count"])
            completed_count = int(phase["completed_count"] or 0)
            active_count = int(phase["active_count"] or 0)
            if task_count and completed_count == task_count:
                status = "completed"
            elif active_count:
                status = "active"
            else:
                status = "pending"
            if status != "completed":
                all_completed = False
            connection.execute(
                "UPDATE plan_phases SET status = ? WHERE id = ? AND plan_id = ?",
                (status, phase["id"], plan_id),
            )
        connection.execute(
            """
            UPDATE plans
            SET status = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ? AND status <> 'archived'
            """,
            ("completed" if all_completed else "active", plan_id, user_pk),
        )


def list_plans(user_id: str, *, status: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    if status is not None and status not in PLAN_STATUSES:
        raise ValueError("unsupported plan status")
    if not 1 <= limit <= 500 or offset < 0:
        raise ValueError("limit must be 1..500 and offset must be non-negative")
    connection = db.get_connection()
    try:
        user_pk = db._ensure_user(connection, db._validate_user_id(user_id))
        sql = "SELECT * FROM plans WHERE user_id = ?"
        params: list[Any] = [user_pk]
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))
        return [dict(row) for row in connection.execute(sql, params)]
    finally:
        connection.close()


def _create_task_in_transaction(
    connection: sqlite3.Connection,
    user_pk: int,
    task: dict[str, Any],
    batch_id: str,
) -> int:
    if task.get("existing_task_id") is not None:
        row = connection.execute(
            "SELECT id FROM tasks WHERE id = ? AND user_id = ? AND deleted_at IS NULL",
            (task["existing_task_id"], user_pk),
        ).fetchone()
        if row is None:
            raise PlanDraftError("existing task does not belong to the current user")
        return int(row["id"])
    profile = connection.execute("SELECT timezone FROM user_profiles WHERE user_id = ?", (user_pk,)).fetchone()
    timezone_name = task.get("timezone") or (profile["timezone"] if profile else db.DEFAULT_USER_TIMEZONE)
    planned = db._normalize_timestamp(task.get("planned_start_at"), timezone_name=timezone_name, field_name="planned_start_at")
    due = db._normalize_timestamp(task.get("due_at"), timezone_name=timezone_name, field_name="due_at")
    cursor = connection.execute(
        """
        INSERT INTO tasks(user_id, title, description, planned_start_at, due_at, source, request_id)
        VALUES (?, ?, ?, ?, ?, 'plan', ?)
        """,
        (user_pk, task["title"], task.get("description"), planned, due, batch_id),
    )
    task_id = int(cursor.lastrowid)
    recurrence = task.get("recurrence")
    if recurrence:
        if not isinstance(recurrence, dict):
            raise PlanDraftError("task.recurrence must be an object")
        frequency = str(recurrence.get("frequency", "custom")).lower()
        if frequency not in {"daily", "weekly", "monthly", "custom"}:
            raise PlanDraftError("unsupported recurrence frequency")
        start_date = _date_text(recurrence.get("start_date") or date.today().isoformat(), "recurrence.start_date")
        end_date = _date_text(recurrence.get("end_date"), "recurrence.end_date")
        interval = int(recurrence.get("interval_value", 1))
        if interval < 1 or (end_date is not None and end_date < start_date):
            raise PlanDraftError("invalid recurrence range or interval")
        connection.execute(
            """
            INSERT INTO recurrence_rules(user_id, task_id, frequency, interval_value, start_date, end_date, timezone, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (user_pk, task_id, frequency, interval, start_date, end_date, timezone_name),
        )
    return task_id


def _apply_payload(
    connection: sqlite3.Connection,
    user_pk: int,
    payload: dict[str, Any],
    *,
    batch_id: str,
    session_id: int | None,
) -> dict[str, Any]:
    base_plan_id = payload.get("base_plan_id")
    version = 1
    if base_plan_id is not None:
        base = _plan_row(connection, user_pk, int(base_plan_id))
        if base is None:
            raise PlanNotFoundError("base plan was not found")
        version = int(base["version"]) + 1
        connection.execute(
            "UPDATE plans SET status = 'archived', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND user_id = ?",
            (base_plan_id, user_pk),
        )
        connection.execute(
            "UPDATE plan_phases SET status = 'archived' WHERE plan_id = ?",
            (base_plan_id,),
        )
        connection.execute(
            """
            UPDATE recurrence_rules SET active = 0
            WHERE user_id = ? AND task_id IN (SELECT task_id FROM plan_tasks WHERE plan_id = ?)
            """,
            (user_pk, base_plan_id),
        )
    cursor = connection.execute(
        """
        INSERT INTO plans(user_id, title, goal, status, source_session_id, version, batch_id, milestones_json)
        VALUES (?, ?, ?, 'active', ?, ?, ?, ?)
        """,
        (user_pk, payload["title"], payload["goal"], session_id, version, batch_id, _canonical(payload.get("milestones", []))),
    )
    plan_id = int(cursor.lastrowid)
    task_ids: dict[str, int] = {}
    phase_ids: list[int] = []
    created_task_ids: list[int] = []
    for phase_index, phase in enumerate(payload["phases"], start=1):
        phase_cursor = connection.execute(
            """
            INSERT INTO plan_phases(plan_id, name, sequence_no, start_date, end_date)
            VALUES (?, ?, ?, ?, ?)
            """,
            (plan_id, phase["name"], phase_index, phase.get("start_date"), phase.get("end_date")),
        )
        phase_id = int(phase_cursor.lastrowid)
        phase_ids.append(phase_id)
        for task_index, task in enumerate(phase["tasks"], start=1):
            task_id = _create_task_in_transaction(connection, user_pk, task, batch_id)
            task_ids[task["key"]] = task_id
            created_task_ids.append(task_id)
            connection.execute(
                """
                INSERT INTO plan_tasks(plan_id, phase_id, task_id, relation_type, sequence_no)
                VALUES (?, ?, ?, ?, ?)
                """,
                (plan_id, phase_id, task_id, task["relation_type"], task_index),
            )
    for dependency in payload["dependencies"]:
        connection.execute(
            """
            INSERT INTO plan_dependencies(plan_id, predecessor_task_id, successor_task_id, dependency_type)
            VALUES (?, ?, ?, ?)
            """,
            (plan_id, task_ids[dependency["predecessor"]], task_ids[dependency["successor"]], dependency["dependency_type"]),
        )
    return {
        "plan_id": plan_id,
        "version": version,
        "status": "active",
        "phase_ids": phase_ids,
        "created_task_ids": created_task_ids,
        "dependency_count": len(payload["dependencies"]),
    }


def apply_plan(user_id: str, draft_id: int, confirmation_token: str) -> dict[str, Any]:
    connection = db.get_connection()
    try:
        user_pk = db._ensure_user(connection, db._validate_user_id(user_id))
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM plan_drafts WHERE id = ? AND user_id = ?", (draft_id, user_pk)
        ).fetchone()
        if row is None:
            raise PlanNotFoundError("plan draft was not found")
        if row["status"] == "applied" and row["applied_plan_id"]:
            connection.commit()
            return {"plan_id": int(row["applied_plan_id"]), "status": "active", "idempotent": True}
        if row["status"] != "draft":
            raise PlanDraftError(f"draft is not applicable in {row['status']} state")
        now = _utc_now()
        if str(row["expires_at"]) <= _utc_text(now):
            connection.execute("UPDATE plan_drafts SET status = 'expired' WHERE id = ?", (draft_id,))
            connection.commit()
            raise PlanDraftError("plan draft has expired")
        if not hmac.compare_digest(_token_digest(str(confirmation_token)), str(row["confirmation_digest"])):
            raise PlanDraftError("confirmation token is invalid")
        payload = json.loads(row["payload_json"])
        _validate_dag(payload)
        result = _apply_payload(
            connection,
            user_pk,
            payload,
            batch_id=str(row["request_id"]),
            session_id=row["session_id"],
        )
        connection.execute(
            """
            UPDATE plan_drafts
            SET status = 'applied', applied_plan_id = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
            WHERE id = ? AND user_id = ? AND status = 'draft'
            """,
            (result["plan_id"], draft_id, user_pk),
        )
        connection.commit()
        result["idempotent"] = False
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def archive_plan(user_id: str, plan_id: int) -> bool:
    connection = db.get_connection()
    try:
        user_pk = db._ensure_user(connection, db._validate_user_id(user_id))
        if _plan_row(connection, user_pk, plan_id) is None:
            return False
        cursor = connection.execute(
            "UPDATE plans SET status = 'archived', updated_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ? AND user_id = ? AND status <> 'archived'",
            (plan_id, user_pk),
        )
        connection.execute(
            "UPDATE plan_phases SET status = 'archived' WHERE plan_id = ?",
            (plan_id,),
        )
        connection.execute(
            """
            UPDATE recurrence_rules SET active = 0
            WHERE user_id = ? AND task_id IN (SELECT task_id FROM plan_tasks WHERE plan_id = ?)
            """,
            (user_pk, plan_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    finally:
        connection.close()


__all__ = [
    "DEPENDENCY_TYPES",
    "DRAFT_STATUSES",
    "PLAN_STATUSES",
    "PlanDraftError",
    "PlanNotFoundError",
    "apply_plan",
    "archive_plan",
    "create_plan_draft",
    "get_plan",
    "list_plans",
    "refresh_plan_progress_for_task",
]
