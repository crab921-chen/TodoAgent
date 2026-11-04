"""Framework-neutral Todo tools for the M2 Harness integration."""

from __future__ import annotations

from typing import Any

from database.db import (
    InvalidTaskStatusError,
    TaskTransactionLinkNotFoundError,
    TransactionNotFoundError,
    RecurrenceNotFoundError,
    ReminderNotFoundError,
    ReminderStateError,
    TaskNotFoundError,
    add_task,
    create_goal_progress,
    create_recurrence_rule,
    list_recurrence_rules,
    create_reminder,
    complete_task,
    get_goal_progress,
    delete_task,
    get_all_tasks,
    get_task_occurrences,
    list_goal_progress,
    list_reminders,
    search_tasks,
    update_task,
    update_recurrence_rule,
    update_reminder,
    cancel_reminder,
    create_task_transaction_link,
    create_transaction,
    delete_task_transaction_link,
    get_task_transaction_link,
    get_transaction,
    list_task_transaction_links,
    list_transactions,
    query_statistics,
    update_transaction,
    void_transaction,
    get_user_timezone,
    set_user_timezone,
    transition_task,
)
from database.m4 import list_tool_audits
from database.exports import ExportError, create_user_export, list_user_exports
from database.charts import ChartNotFoundError, ChartSpecError, build_chart
from database.plans import (
    PlanDraftError,
    PlanNotFoundError,
    apply_plan,
    archive_plan,
    create_plan_draft,
    get_plan,
    list_plans,
)
from ops.healthcheck import check_health
from harness.tool_protocol import (
    PermissionLevel,
    ToolCallContext,
    ToolContract,
    ToolExecutionError,
    ToolSpec,
)
from harness.tool_registry import ToolRegistry
from harness.conversation_tools import conversation_tool_contracts
from harness.tool_models import (
    _StrictModel,
    AddTaskInput,
    AuditListInput,
    BackupExportInput,
    BackupExportListInput,
    ChartBuildInput,
    ChartSpec,
    CompleteTaskInput,
    GoalProgressCreateInput,
    GoalProgressGetInput,
    GoalProgressListInput,
    ListOccurrencesInput,
    ListTasksInput,
    RecurrenceCreateInput,
    RecurrenceListInput,
    RecurrenceUpdateInput,
    ReminderCancelInput,
    ReminderCreateInput,
    ReminderListInput,
    ReminderUpdateInput,
    SearchTasksInput,
    StatisticsQueryInput,
    TaskIdInput,
    TaskCreateInput,
    TaskTransitionInput,
    PlanApplyInput,
    PlanDraftInput,
    PlanIdInput,
    PlanListInput,
    TaskTransactionLinkGetInput,
    TaskTransactionLinkInput,
    TaskTransactionLinkListInput,
    ToolData,
    TransactionCreateInput,
    TransactionGetInput,
    TransactionListInput,
    TransactionUpdateInput,
    TransactionVoidInput,
    UpdateTaskInput,
    UserTimezoneSetInput,
)


def _data(value: Any) -> dict[str, Any]:
    return {"data": value}


def _add_task(arguments: AddTaskInput, context: ToolCallContext) -> dict[str, Any]:
    return _data(
        add_task(
            context.user_id,
            arguments.title,
            arguments.description,
            planned_start_at=arguments.planned_start_at,
            due_at=arguments.due_at,
            source="agent",
            request_id=context.request_id,
        )
    )


def _create_task(arguments: TaskCreateInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            add_task(
                context.user_id,
                arguments.title,
                arguments.description,
                planned_start_at=arguments.planned_start_at,
                due_at=arguments.due_at,
                timezone_name=arguments.timezone,
                source=arguments.source,
                request_id=context.request_id,
                include_metadata=True,
            )
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _transition_task(
    arguments: TaskTransitionInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        return _data(
            transition_task(
                context.user_id,
                arguments.task_id,
                arguments.action,
                request_id=context.request_id,
                source="agent",
                occurred_at=arguments.occurred_at,
                notes=arguments.notes,
            )
        )
    except TaskNotFoundError as error:
        raise ToolExecutionError(
            "TASK_NOT_FOUND",
            "Task was not found for the authenticated user.",
        ) from error
    except (InvalidTaskStatusError, ValueError) as error:
        raise ToolExecutionError("INVALID_TASK_STATE", str(error)) from error


def _archive_task(arguments: TaskIdInput, context: ToolCallContext) -> dict[str, Any]:
    if not delete_task(context.user_id, arguments.task_id):
        raise ToolExecutionError(
            "TASK_NOT_FOUND",
            "Task was not found for the authenticated user.",
        )
    return _data({"task_id": arguments.task_id, "status": "archived"})


def _list_tasks(arguments: ListTasksInput, context: ToolCallContext) -> dict[str, Any]:
    return _data(
        get_all_tasks(context.user_id, limit=arguments.limit, offset=arguments.offset)
    )


def _search_tasks(arguments: SearchTasksInput, context: ToolCallContext) -> dict[str, Any]:
    return _data(
        search_tasks(
            context.user_id,
            arguments.query,
            limit=arguments.limit,
            offset=arguments.offset,
        )
    )


def _update_task(arguments: UpdateTaskInput, context: ToolCallContext) -> dict[str, Any]:
    task = update_task(
        context.user_id,
        arguments.task_id,
        title=arguments.title,
        description=arguments.description,
        status=arguments.status,
        planned_start_at=arguments.planned_start_at,
        due_at=arguments.due_at,
    )
    if task is None:
        raise ToolExecutionError(
            "TASK_NOT_FOUND",
            "Task was not found for the authenticated user.",
        )
    return _data(task)


def _delete_task(arguments: TaskIdInput, context: ToolCallContext) -> dict[str, Any]:
    deleted = delete_task(context.user_id, arguments.task_id)
    if not deleted:
        raise ToolExecutionError(
            "TASK_NOT_FOUND",
            "Task was not found for the authenticated user.",
        )
    return _data({"task_id": arguments.task_id, "deleted": True})


def _complete_task(
    arguments: CompleteTaskInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        idempotency_key = arguments.idempotency_key or f"{context.request_id}:complete"
        return _data(
            complete_task(
                context.user_id,
                arguments.task_id,
                occurred_at=arguments.occurred_at,
                notes=arguments.notes,
                idempotency_key=idempotency_key,
            )
        )
    except TaskNotFoundError as error:
        raise ToolExecutionError(
            "TASK_NOT_FOUND",
            "Task was not found for the authenticated user.",
        ) from error


def _list_occurrences(
    arguments: ListOccurrencesInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    return _data(
        get_task_occurrences(
            context.user_id,
            arguments.task_id,
            start_at=arguments.start_at,
            end_at=arguments.end_at,
            period=arguments.period,
            reference_at=arguments.reference_at,
            status=arguments.status,
            limit=arguments.limit,
            offset=arguments.offset,
        )
    )


def _m3_not_found(code: str, message: str) -> ToolExecutionError:
    return ToolExecutionError(code, message)


def _create_reminder(arguments: ReminderCreateInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            create_reminder(
                context.user_id,
                arguments.content,
                arguments.trigger_at,
                task_id=arguments.task_id,
                timezone_name=arguments.timezone,
                source=arguments.source,
            )
        )
    except TaskNotFoundError as error:
        raise _m3_not_found("TASK_NOT_FOUND", "Task was not found for the authenticated user.") from error
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _update_reminder(arguments: ReminderUpdateInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            update_reminder(
                context.user_id,
                arguments.reminder_id,
                content=arguments.content,
                trigger_at=arguments.trigger_at,
                timezone_name=arguments.timezone,
            )
        )
    except ReminderNotFoundError as error:
        raise _m3_not_found("REMINDER_NOT_FOUND", "Reminder was not found for the authenticated user.") from error
    except ReminderStateError as error:
        raise ToolExecutionError("INVALID_REMINDER_STATE", str(error)) from error
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _cancel_reminder(arguments: ReminderCancelInput, context: ToolCallContext) -> dict[str, Any]:
    if not cancel_reminder(context.user_id, arguments.reminder_id):
        raise _m3_not_found("REMINDER_NOT_FOUND", "Reminder was not found for the authenticated user.")
    return _data({"reminder_id": arguments.reminder_id, "cancelled": True})


def _list_reminders(arguments: ReminderListInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            list_reminders(
                context.user_id,
                status=arguments.status,
                start_at=arguments.start_at,
                end_at=arguments.end_at,
                include_source=arguments.include_source,
                limit=arguments.limit,
                offset=arguments.offset,
            )
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _create_recurrence(arguments: RecurrenceCreateInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            create_recurrence_rule(
                context.user_id,
                arguments.task_id,
                arguments.frequency,
                arguments.start_date,
                interval_value=arguments.interval_value,
                end_date=arguments.end_date,
                timezone_name=arguments.timezone,
                active=arguments.active,
            )
        )
    except TaskNotFoundError as error:
        raise _m3_not_found("TASK_NOT_FOUND", "Task was not found for the authenticated user.") from error
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _update_recurrence(arguments: RecurrenceUpdateInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            update_recurrence_rule(
                context.user_id,
                arguments.rule_id,
                frequency=arguments.frequency,
                interval_value=arguments.interval_value,
                start_date=arguments.start_date,
                end_date=arguments.end_date,
                timezone_name=arguments.timezone,
                active=arguments.active,
            )
        )
    except RecurrenceNotFoundError as error:
        raise _m3_not_found("RECURRENCE_NOT_FOUND", "Recurrence rule was not found for the authenticated user.") from error
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _list_recurrence(arguments: RecurrenceListInput, context: ToolCallContext) -> dict[str, Any]:
    return _data(
        list_recurrence_rules(
            context.user_id,
            active=arguments.active,
            task_id=arguments.task_id,
        )
    )


def _create_goal(arguments: GoalProgressCreateInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            create_goal_progress(
                context.user_id,
                arguments.task_id,
                arguments.period_start,
                arguments.period_end,
                arguments.target_count,
            )
        )
    except TaskNotFoundError as error:
        raise _m3_not_found("TASK_NOT_FOUND", "Task was not found for the authenticated user.") from error
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _get_goal(arguments: GoalProgressGetInput, context: ToolCallContext) -> dict[str, Any]:
    goal = get_goal_progress(context.user_id, arguments.goal_id)
    if goal is None:
        raise _m3_not_found("GOAL_NOT_FOUND", "Goal was not found for the authenticated user.")
    return _data(goal)


def _list_goals(arguments: GoalProgressListInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            list_goal_progress(
                context.user_id,
                task_id=arguments.task_id,
                status=arguments.status,
            )
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _create_transaction(
    arguments: TransactionCreateInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        return _data(
            create_transaction(
                context.user_id,
                arguments.type,
                arguments.amount_minor,
                arguments.category,
                currency=arguments.currency,
                note=arguments.note,
                occurred_at=arguments.occurred_at,
            )
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _get_transaction(
    arguments: TransactionGetInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    transaction = get_transaction(
        context.user_id,
        arguments.transaction_id,
        include_deleted=arguments.include_deleted,
    )
    if transaction is None:
        raise ToolExecutionError(
            "TRANSACTION_NOT_FOUND",
            "Transaction was not found for the authenticated user.",
        )
    return _data(transaction)


def _list_transactions(
    arguments: TransactionListInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        return _data(
            list_transactions(
                context.user_id,
                transaction_type=arguments.type,
                category=arguments.category,
                currency=arguments.currency,
                start_at=arguments.start_at,
                end_at=arguments.end_at,
                include_deleted=arguments.include_deleted,
                limit=arguments.limit,
                offset=arguments.offset,
            )
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _update_transaction(
    arguments: TransactionUpdateInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        transaction = update_transaction(
            context.user_id,
            arguments.transaction_id,
            transaction_type=arguments.type,
            amount_minor=arguments.amount_minor,
            currency=arguments.currency,
            category=arguments.category,
            note=arguments.note,
            occurred_at=arguments.occurred_at,
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error
    if transaction is None:
        raise ToolExecutionError(
            "TRANSACTION_NOT_FOUND",
            "Transaction was not found for the authenticated user.",
        )
    return _data(transaction)


def _void_transaction(
    arguments: TransactionVoidInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    if not void_transaction(context.user_id, arguments.transaction_id):
        raise ToolExecutionError(
            "TRANSACTION_NOT_FOUND",
            "Transaction was not found for the authenticated user.",
        )
    return _data({"transaction_id": arguments.transaction_id, "voided": True})


def _create_task_transaction_link(
    arguments: TaskTransactionLinkInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        return _data(
            create_task_transaction_link(
                context.user_id,
                arguments.task_id,
                arguments.transaction_id,
                basis=arguments.basis,
                confirmed_at=arguments.confirmed_at,
            )
        )
    except TaskTransactionLinkNotFoundError as error:
        raise ToolExecutionError(
            "LINK_NOT_FOUND",
            "Task or transaction was not found for the authenticated user.",
        ) from error
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _get_task_transaction_link(
    arguments: TaskTransactionLinkGetInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    link = get_task_transaction_link(context.user_id, arguments.link_id)
    if link is None:
        raise ToolExecutionError(
            "LINK_NOT_FOUND",
            "Task transaction link was not found for the authenticated user.",
        )
    return _data(link)


def _list_task_transaction_links(
    arguments: TaskTransactionLinkListInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    return _data(
        list_task_transaction_links(
            context.user_id,
            task_id=arguments.task_id,
            transaction_id=arguments.transaction_id,
        )
    )


def _delete_task_transaction_link(
    arguments: TaskTransactionLinkGetInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    if not delete_task_transaction_link(context.user_id, arguments.link_id):
        raise ToolExecutionError(
            "LINK_NOT_FOUND",
            "Task transaction link was not found for the authenticated user.",
        )
    return _data({"link_id": arguments.link_id, "deleted": True})


def _statistics_query(
    arguments: StatisticsQueryInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        return _data(
            query_statistics(
                context.user_id,
                period=arguments.period,
                range_start=arguments.range_start,
                range_end=arguments.range_end,
                reference_at=arguments.reference_at,
                task_id=arguments.task_id,
                status=arguments.status,
                transaction_type=arguments.type,
                category=arguments.category,
                currency=arguments.currency,
                session_id=(
                    int(context.session_key.split(":", 1)[1])
                    if context.session_key and context.session_key.startswith("conversation:")
                    and context.session_key.split(":", 1)[1].isdigit()
                    else None
                ),
            )
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _chart_build(arguments: ChartBuildInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        result = build_chart(
            context.user_id,
            chart_type=arguments.chart_type,
            dataset_ref=arguments.dataset_ref,
            dimensions=arguments.dimensions,
            metric=arguments.metric,
            query_digest=arguments.query_digest,
            title=arguments.title,
        )
        result["spec"] = ChartSpec.model_validate(result["spec"]).model_dump(mode="json")
        return _data(result)
    except ChartNotFoundError as error:
        raise ToolExecutionError("CHART_NOT_FOUND", str(error)) from error
    except ChartSpecError as error:
        raise ToolExecutionError("INVALID_CHART_SPEC", str(error)) from error


def _get_timezone(arguments: _StrictModel, context: ToolCallContext) -> dict[str, Any]:
    return _data({"timezone": get_user_timezone(context.user_id)})


def _set_timezone(
    arguments: UserTimezoneSetInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        return _data({"timezone": set_user_timezone(context.user_id, arguments.timezone)})
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _list_audits(
    arguments: AuditListInput,
    context: ToolCallContext,
) -> dict[str, Any]:
    try:
        return _data(
            list_tool_audits(
                context.user_id,
                request_id=arguments.request_id,
                limit=arguments.limit,
            )
        )
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _backup_export(arguments: BackupExportInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            create_user_export(
                context.user_id,
                scope=arguments.scope,
                export_format=arguments.format,
                request_id=context.request_id,
                ttl_seconds=arguments.ttl_seconds,
            )
        )
    except ExportError as error:
        raise ToolExecutionError("EXPORT_FAILED", str(error)) from error


def _backup_export_list(arguments: BackupExportListInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(list_user_exports(context.user_id, limit=arguments.limit))
    except ExportError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _runtime_health(arguments: _StrictModel, context: ToolCallContext) -> dict[str, Any]:
    return _data(check_health())


def _plan_draft(arguments: PlanDraftInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(
            create_plan_draft(
                context.user_id,
                intent=arguments.intent,
                constraints=arguments.constraints,
                existing_context=arguments.existing_context,
                request_id=context.request_id,
                session_id=arguments.session_id,
                ttl_seconds=arguments.ttl_seconds,
            )
        )
    except (PlanDraftError, ValueError) as error:
        raise ToolExecutionError("INVALID_PLAN_DRAFT", str(error)) from error


def _plan_apply(arguments: PlanApplyInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(apply_plan(context.user_id, arguments.draft_id, arguments.confirmation_token))
    except PlanNotFoundError as error:
        raise ToolExecutionError("PLAN_NOT_FOUND", str(error)) from error
    except PlanDraftError as error:
        raise ToolExecutionError("PLAN_CONFIRMATION_FAILED", str(error)) from error


def _plan_get(arguments: PlanIdInput, context: ToolCallContext) -> dict[str, Any]:
    plan = get_plan(context.user_id, arguments.plan_id)
    if plan is None:
        raise ToolExecutionError("PLAN_NOT_FOUND", "Plan was not found for the authenticated user.")
    return _data(plan)


def _plan_list(arguments: PlanListInput, context: ToolCallContext) -> dict[str, Any]:
    try:
        return _data(list_plans(
            context.user_id,
            status=arguments.status,
            limit=arguments.limit,
            offset=arguments.offset,
        ))
    except ValueError as error:
        raise ToolExecutionError("INVALID_ARGUMENTS", str(error)) from error


def _plan_archive(arguments: PlanIdInput, context: ToolCallContext) -> dict[str, Any]:
    if not archive_plan(context.user_id, arguments.plan_id):
        raise ToolExecutionError("PLAN_NOT_FOUND", "Plan was not found for the authenticated user.")
    return _data({"plan_id": arguments.plan_id, "status": "archived"})


def todo_tool_contracts() -> tuple[ToolContract, ...]:
    """Build the standard M2 Todo contracts in stable model-facing order."""

    return (
        ToolContract(
            ToolSpec(
                name="task_create",
                description="Create a task through the unified task entry point.",
                input_model=TaskCreateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _create_task,
        ),
        ToolContract(
            ToolSpec(
                name="task_transition",
                description="Start, pause, complete, or cancel an owned task.",
                input_model=TaskTransitionInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _transition_task,
        ),
        ToolContract(
            ToolSpec(
                name="task_archive",
                description="Archive an owned task after explicit confirmation.",
                input_model=TaskIdInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _archive_task,
        ),
        ToolContract(
            ToolSpec(
                name="todo_add_task",
                description="Create a task for the authenticated user.",
                input_model=AddTaskInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _add_task,
        ),
        ToolContract(
            ToolSpec(
                name="todo_list_tasks",
                description="List active tasks for the authenticated user.",
                input_model=ListTasksInput,
                output_model=ToolData,
            ),
            _list_tasks,
        ),
        ToolContract(
            ToolSpec(
                name="todo_search_tasks",
                description="Search the authenticated user's active tasks.",
                input_model=SearchTasksInput,
                output_model=ToolData,
            ),
            _search_tasks,
        ),
        ToolContract(
            ToolSpec(
                name="todo_update_task",
                description="Update an owned task using the task state machine.",
                input_model=UpdateTaskInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _update_task,
        ),
        ToolContract(
            ToolSpec(
                name="todo_delete_task",
                description="Archive an owned task after explicit confirmation.",
                input_model=TaskIdInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _delete_task,
        ),
        ToolContract(
            ToolSpec(
                name="todo_complete_task",
                description="Record one completed execution of an owned task.",
                input_model=CompleteTaskInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _complete_task,
        ),
        ToolContract(
            ToolSpec(
                name="todo_list_occurrences",
                description="List execution history for an owned task.",
                input_model=ListOccurrencesInput,
                output_model=ToolData,
            ),
            _list_occurrences,
        ),
        ToolContract(
            ToolSpec(
                name="reminder_create",
                description="Create a persistent reminder for the authenticated user.",
                input_model=ReminderCreateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _create_reminder,
        ),
        ToolContract(
            ToolSpec(
                name="reminder_update",
                description="Update a reminder that has not reached a terminal state.",
                input_model=ReminderUpdateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _update_reminder,
        ),
        ToolContract(
            ToolSpec(
                name="reminder_cancel",
                description="Cancel an owned reminder before delivery.",
                input_model=ReminderCancelInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _cancel_reminder,
        ),
        ToolContract(
            ToolSpec(
                name="reminder_list",
                description="List persistent reminders by status and time range.",
                input_model=ReminderListInput,
                output_model=ToolData,
            ),
            _list_reminders,
        ),
        ToolContract(
            ToolSpec(
                name="recurrence_create",
                description="Create a daily, weekly, monthly, or custom recurrence rule.",
                input_model=RecurrenceCreateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _create_recurrence,
        ),
        ToolContract(
            ToolSpec(
                name="recurrence_update",
                description="Update or pause an owned recurrence rule.",
                input_model=RecurrenceUpdateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _update_recurrence,
        ),
        ToolContract(
            ToolSpec(
                name="recurrence_list",
                description="Check recurrence rules for an owned task.",
                input_model=RecurrenceListInput,
                output_model=ToolData,
            ),
            _list_recurrence,
        ),
        ToolContract(
            ToolSpec(
                name="goal_progress_create",
                description="Create a cumulative task goal for a date period.",
                input_model=GoalProgressCreateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _create_goal,
        ),
        ToolContract(
            ToolSpec(
                name="goal_progress_get",
                description="Read cumulative goal progress from valid occurrences.",
                input_model=GoalProgressGetInput,
                output_model=ToolData,
            ),
            _get_goal,
        ),
        ToolContract(
            ToolSpec(
                name="goal_progress_list",
                description="List cumulative goals for the authenticated user.",
                input_model=GoalProgressListInput,
                output_model=ToolData,
            ),
            _list_goals,
        ),
        ToolContract(
            ToolSpec(
                name="transaction_create",
                description="Create an income or expense for the authenticated user.",
                input_model=TransactionCreateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _create_transaction,
        ),
        ToolContract(
            ToolSpec(
                name="transaction_get",
                description="Read one owned transaction.",
                input_model=TransactionGetInput,
                output_model=ToolData,
            ),
            _get_transaction,
        ),
        ToolContract(
            ToolSpec(
                name="transaction_list",
                description="List owned transactions with optional filters and pagination.",
                input_model=TransactionListInput,
                output_model=ToolData,
            ),
            _list_transactions,
        ),
        ToolContract(
            ToolSpec(
                name="transaction_update",
                description="Update an owned transaction and preserve its entry time.",
                input_model=TransactionUpdateInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _update_transaction,
        ),
        ToolContract(
            ToolSpec(
                name="transaction_void",
                description="Soft-delete an owned transaction after confirmation.",
                input_model=TransactionVoidInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _void_transaction,
        ),
        ToolContract(
            ToolSpec(
                name="transaction_delete",
                description="Soft-delete an owned transaction after confirmation.",
                input_model=TransactionVoidInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _void_transaction,
        ),
        ToolContract(
            ToolSpec(
                name="task_transaction_link",
                description="Explicitly associate one owned task and transaction.",
                input_model=TaskTransactionLinkInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _create_task_transaction_link,
        ),
        ToolContract(
            ToolSpec(
                name="task_transaction_link_get",
                description="Read one owned task transaction link.",
                input_model=TaskTransactionLinkGetInput,
                output_model=ToolData,
            ),
            _get_task_transaction_link,
        ),
        ToolContract(
            ToolSpec(
                name="task_transaction_link_list",
                description="List explicit task transaction links for the user.",
                input_model=TaskTransactionLinkListInput,
                output_model=ToolData,
            ),
            _list_task_transaction_links,
        ),
        ToolContract(
            ToolSpec(
                name="task_transaction_link_delete",
                description="Remove an owned task transaction link.",
                input_model=TaskTransactionLinkGetInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _delete_task_transaction_link,
        ),
        ToolContract(
            ToolSpec(
                name="statistics_query",
                description="Query database-backed task and finance statistics.",
                input_model=StatisticsQueryInput,
                output_model=ToolData,
            ),
            _statistics_query,
        ),
        ToolContract(
            ToolSpec(
                name="chart_build",
                description="Build a validated chart from an owned statistics dataset.",
                input_model=ChartBuildInput,
                output_model=ToolData,
            ),
            _chart_build,
        ),
        ToolContract(
            ToolSpec(
                name="user_timezone_get",
                description="Read the authenticated user's configured timezone.",
                input_model=_StrictModel,
                output_model=ToolData,
            ),
            _get_timezone,
        ),
        ToolContract(
            ToolSpec(
                name="user_timezone_set",
                description="Set the authenticated user's IANA timezone.",
                input_model=UserTimezoneSetInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _set_timezone,
        ),
        ToolContract(
            ToolSpec(
                name="tool_audit_list",
                description="List bounded audit metadata for the authenticated user.",
                input_model=AuditListInput,
                output_model=ToolData,
            ),
            _list_audits,
        ),
        ToolContract(
            ToolSpec(
                name="backup_export",
                description="Export the authenticated user's selected data scope as validated JSON.",
                input_model=BackupExportInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _backup_export,
        ),
        ToolContract(
            ToolSpec(
                name="backup_export_list",
                description="List export manifests owned by the authenticated user.",
                input_model=BackupExportListInput,
                output_model=ToolData,
            ),
            _backup_export_list,
        ),
        ToolContract(
            ToolSpec(
                name="runtime_health",
                description="Return a safe operational health report.",
                input_model=_StrictModel,
                output_model=ToolData,
            ),
            _runtime_health,
        ),
        ToolContract(
            ToolSpec(
                name="plan_draft",
                description="Create a preview-only long-term plan draft.",
                input_model=PlanDraftInput,
                output_model=ToolData,
                permission_level=PermissionLevel.WRITE,
            ),
            _plan_draft,
        ),
        ToolContract(
            ToolSpec(
                name="plan_apply",
                description="Apply a confirmed plan draft atomically.",
                input_model=PlanApplyInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _plan_apply,
        ),
        ToolContract(
            ToolSpec(
                name="plan_get",
                description="Read an owned plan and its phases, tasks, and dependencies.",
                input_model=PlanIdInput,
                output_model=ToolData,
            ),
            _plan_get,
        ),
        ToolContract(
            ToolSpec(
                name="plan_list",
                description="List plans owned by the authenticated user.",
                input_model=PlanListInput,
                output_model=ToolData,
            ),
            _plan_list,
        ),
        ToolContract(
            ToolSpec(
                name="plan_archive",
                description="Archive an owned plan and stop its future recurrence rules.",
                input_model=PlanIdInput,
                output_model=ToolData,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            _plan_archive,
        ),
        *conversation_tool_contracts(),
    )


def build_todo_tool_registry() -> ToolRegistry:
    """Return a fresh registry containing the M2 Todo tools."""

    return ToolRegistry(todo_tool_contracts())


__all__ = [
    "AddTaskInput",
    "TaskCreateInput",
    "TaskTransitionInput",
    "CompleteTaskInput",
    "GoalProgressCreateInput",
    "GoalProgressGetInput",
    "GoalProgressListInput",
    "ListOccurrencesInput",
    "ListTasksInput",
    "SearchTasksInput",
    "TaskIdInput",
    "ToolData",
    "UpdateTaskInput",
    "ReminderCancelInput",
    "ReminderCreateInput",
    "ReminderListInput",
    "ReminderUpdateInput",
    "RecurrenceCreateInput",
    "RecurrenceListInput",
    "RecurrenceUpdateInput",
    "StatisticsQueryInput",
    "AuditListInput",
    "UserTimezoneSetInput",
    "TaskTransactionLinkGetInput",
    "TaskTransactionLinkInput",
    "TaskTransactionLinkListInput",
    "TransactionCreateInput",
    "TransactionGetInput",
    "TransactionListInput",
    "TransactionUpdateInput",
    "TransactionVoidInput",
    "PlanDraftInput",
    "PlanApplyInput",
    "PlanIdInput",
    "PlanListInput",
    "build_todo_tool_registry",
    "todo_tool_contracts",
]
