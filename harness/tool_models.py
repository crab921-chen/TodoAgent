"""Model-visible input and output schemas for business tools.

The schemas are kept separate from handlers and registry construction. Their
definitions are intentionally unchanged so existing tool contracts remain
wire-compatible.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ToolData(_StrictModel):
    data: Any


class AddTaskInput(_StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    planned_start_at: str | None = None
    due_at: str | None = None


class TaskCreateInput(_StrictModel):
    title: str = Field(min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    planned_start_at: str | None = None
    due_at: str | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    source: str = Field(default="agent", min_length=1, max_length=100)


class TaskTransitionInput(_StrictModel):
    task_id: int = Field(gt=0)
    action: Literal["start", "pause", "complete", "cancel"]
    occurred_at: str | None = None
    notes: str | None = Field(default=None, max_length=10000)


class ListTasksInput(_StrictModel):
    limit: int | None = Field(default=None, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class SearchTasksInput(ListTasksInput):
    query: str = Field(min_length=1, max_length=200)


class TaskIdInput(_StrictModel):
    task_id: int = Field(gt=0)


class UpdateTaskInput(TaskIdInput):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=10000)
    status: str | None = None
    planned_start_at: str | None = None
    due_at: str | None = None


class CompleteTaskInput(TaskIdInput):
    occurred_at: str | None = None
    notes: str | None = Field(default=None, max_length=10000)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)


class ListOccurrencesInput(TaskIdInput, ListTasksInput):
    start_at: str | None = None
    end_at: str | None = None
    period: Literal["day", "week", "custom"] = "custom"
    reference_at: str | None = None
    status: Literal["started", "completed", "cancelled"] | None = None


class ReminderCreateInput(_StrictModel):
    content: str = Field(min_length=1, max_length=500)
    trigger_at: str
    task_id: int | None = Field(default=None, gt=0)
    timezone: str | None = None
    source: str = Field(default="agent", min_length=1, max_length=100)


class ReminderUpdateInput(_StrictModel):
    reminder_id: int = Field(gt=0)
    content: str | None = Field(default=None, min_length=1, max_length=500)
    trigger_at: str | None = None
    timezone: str | None = None


class ReminderCancelInput(_StrictModel):
    reminder_id: int = Field(gt=0)


class ReminderListInput(ListTasksInput):
    status: str | None = None
    start_at: str | None = None
    end_at: str | None = None
    include_source: bool = False


class RecurrenceCreateInput(_StrictModel):
    task_id: int = Field(gt=0)
    frequency: str
    start_date: str
    interval_value: int = Field(default=1, gt=0)
    end_date: str | None = None
    timezone: str | None = None
    active: bool = True


class RecurrenceUpdateInput(_StrictModel):
    rule_id: int = Field(gt=0)
    frequency: str | None = None
    interval_value: int | None = Field(default=None, gt=0)
    start_date: str | None = None
    end_date: str | None = None
    timezone: str | None = None
    active: bool | None = None


class RecurrenceListInput(_StrictModel):
    task_id: int | None = Field(default=None, gt=0)
    active: bool | None = None


class GoalProgressCreateInput(_StrictModel):
    task_id: int = Field(gt=0)
    period_start: str
    period_end: str
    target_count: int = Field(gt=0)


class GoalProgressGetInput(_StrictModel):
    goal_id: int = Field(gt=0)


class GoalProgressListInput(_StrictModel):
    task_id: int | None = Field(default=None, gt=0)
    status: str | None = None


class TransactionCreateInput(_StrictModel):
    type: str
    amount_minor: StrictInt = Field(gt=0)
    category: str = Field(min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    note: str | None = Field(default=None, max_length=10000)
    occurred_at: str | None = None


class TransactionGetInput(_StrictModel):
    transaction_id: int = Field(gt=0)
    include_deleted: bool = False


class TransactionListInput(ListTasksInput):
    type: str | None = None
    category: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    start_at: str | None = None
    end_at: str | None = None
    include_deleted: bool = False


class TransactionUpdateInput(_StrictModel):
    transaction_id: int = Field(gt=0)
    type: str | None = None
    amount_minor: int | None = Field(default=None, gt=0)
    category: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)
    note: str | None = Field(default=None, max_length=10000)
    occurred_at: str | None = None


class TransactionVoidInput(_StrictModel):
    transaction_id: int = Field(gt=0)


class BackupExportInput(_StrictModel):
    scope: Literal["all", "tasks", "plans", "finance", "conversations"] = "all"
    format: Literal["json"] = "json"
    ttl_seconds: int = Field(default=86400, ge=60, le=604800)


class BackupExportListInput(_StrictModel):
    limit: int = Field(default=50, ge=1, le=500)


class TaskTransactionLinkInput(_StrictModel):
    task_id: int = Field(gt=0)
    transaction_id: int = Field(gt=0)
    basis: str = "user_confirmed"
    confirmed_at: str | None = None


class TaskTransactionLinkGetInput(_StrictModel):
    link_id: int = Field(gt=0)


class TaskTransactionLinkListInput(_StrictModel):
    task_id: int | None = Field(default=None, gt=0)
    transaction_id: int | None = Field(default=None, gt=0)


class StatisticsQueryInput(_StrictModel):
    period: Literal["day", "week", "month", "year", "custom"] = "custom"
    range_start: str | None = None
    range_end: str | None = None
    reference_at: str | None = None
    task_id: int | None = Field(default=None, gt=0)
    status: Literal["pending", "in_progress", "completed", "cancelled", "archived"] | None = None
    type: str | None = None
    category: str | None = Field(default=None, min_length=1, max_length=100)
    currency: str | None = Field(default=None, min_length=3, max_length=3)


class ChartBuildInput(_StrictModel):
    chart_type: Literal["line", "bar", "pie", "table"]
    dataset_ref: str = Field(min_length=1, max_length=100)
    dimensions: list[str] = Field(min_length=1, max_length=3)
    metric: str = Field(min_length=1, max_length=50)
    query_digest: str = Field(min_length=64, max_length=64)
    title: str | None = Field(default=None, min_length=1, max_length=200)


class ChartSpec(_StrictModel):
    version: Literal[1]
    chart_type: Literal["line", "bar", "pie", "table"]
    dataset_ref: str = Field(min_length=1, max_length=100)
    query_digest: str = Field(min_length=64, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    dimensions: list[str] = Field(min_length=1, max_length=3)
    metric: str = Field(min_length=1, max_length=50)
    timezone: str = Field(min_length=1, max_length=100)
    range_start: str = Field(min_length=1, max_length=100)
    range_end: str = Field(min_length=1, max_length=100)
    data: list[dict[str, Any]]


class UserTimezoneSetInput(_StrictModel):
    timezone: str = Field(min_length=1, max_length=100)


class AuditListInput(_StrictModel):
    request_id: str | None = Field(default=None, max_length=200)
    limit: int = Field(default=100, ge=1, le=500)


class ConversationCreateInput(_StrictModel):
    title: str = Field(min_length=1, max_length=200)
    client_request_id: str | None = Field(default=None, min_length=1, max_length=200)


class ConversationListInput(_StrictModel):
    status: Literal["active", "archived"] | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class ConversationMessagesInput(_StrictModel):
    session_id: int = Field(gt=0)
    limit: int = Field(default=100, ge=1, le=500)
    offset: int = Field(default=0, ge=0)


class ConversationRenameInput(_StrictModel):
    session_id: int = Field(gt=0)
    title: str = Field(min_length=1, max_length=200)


class ConversationArchiveInput(_StrictModel):
    session_id: int = Field(gt=0)


class MessageAppendInput(_StrictModel):
    session_id: int = Field(gt=0)
    role: Literal["user", "assistant", "tool", "system"]
    content: Any = None
    message_type: str = Field(default="message", min_length=1, max_length=50)
    client_message_id: str | None = Field(default=None, min_length=1, max_length=200)
    tool_call_id: str | None = Field(default=None, max_length=200)
    metadata: dict[str, Any] | None = None


class PlanDraftInput(_StrictModel):
    intent: str = Field(min_length=1, max_length=10000)
    constraints: dict[str, Any] | None = None
    existing_context: dict[str, Any] | None = None
    session_id: int | None = Field(default=None, gt=0)
    ttl_seconds: int = Field(default=86400, ge=60, le=604800)


class PlanApplyInput(_StrictModel):
    draft_id: int = Field(gt=0)
    confirmation_token: str = Field(min_length=1, max_length=500)


class PlanIdInput(_StrictModel):
    plan_id: int = Field(gt=0)


class PlanListInput(_StrictModel):
    status: Literal["draft", "confirmed", "active", "completed", "archived"] | None = None
    limit: int = Field(default=50, ge=1, le=500)
    offset: int = Field(default=0, ge=0)
