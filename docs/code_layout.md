# Code Layout

The repository contains two deliberately separated execution layers.

## Improved runtime

These directories contain the maintained implementation delivered in M1-M5:

* `harness/`: framework-neutral Agent Loop, tool protocol, permissions,
  confirmation, budgets, cancellation, hooks, and business tool registry.
* `database/`: migrations, task/occurrence services, reminders, recurrence,
  goals, finance, statistics, audit, backup, restore, and reconciliation.
* `ops/`: runtime configuration, health checks, structured redacting logs, and
  local runtime metrics.
* `tests/test_*.py`: automated unit, integration, end-to-end, security,
  concurrency, recovery, and operations tests.
* `tests/legacy/`: archived thread-isolation and checkpoint inspection
  scripts from the original repository.

## Original repository implementation

The original LangGraph/LangChain implementation remains available under:

* `legacy/langgraph_app/`: original graph, context, state, routing, and model
  adapter.
* `legacy/langgraph_tools/`: original task tools used by that graph.
* `legacy/langgraph_prompts/`: original system prompt.

This code is retained for compatibility, comparison, and archived diagnostics.
The public Streamlit and CLI experiences do not use this conversation path.
They use `harness.AgentLoop` and the framework-neutral tool registry.

## Compatibility paths

The historical imports remain available through thin wrappers:

* `graph/` re-exports `legacy.langgraph_app`.
* `tools/add_task.py`, `tools/view_tasks.py`, and the other task modules
  re-export `legacy.langgraph_tools`.
* `prompts/system_prompt.py` re-exports the legacy prompt.

This keeps existing entrypoints and third-party imports stable while making the
ownership boundary visible. New M1-M5 work should use `harness/`, `database/`,
and `ops/`, not add new code to the compatibility wrappers.

The historical diagnostic paths `tests/test_threads.py`,
`tests/inspect_state.py`, and `tests/inspect_history.py` remain as thin
compatibility entrypoints for their archived implementations in `tests/legacy/`.

## Entry points

* `app.py`: the unified Streamlit UI with database initialization,
  configuration, and structured workspace integration. It is backed by the
  Harness Agent/Tool protocol.
* `cli.py`: terminal conversation entrypoint backed by the unified Harness
  Agent/Tool protocol.
* `ops/startup_check.py`: migration-safe startup gate.
* `ops/healthcheck.py`: read-only health report.
* `tools/release_signoff.py`: complete M5 release evidence.

## Maintained framework layers

The maintained implementation is organized by responsibility. Existing
database modules remain in place because moving them would change import paths
and increase compatibility risk; their logical ownership is documented below.

* **Entry layer**: `app.py`, `cli.py`, `ops/startup_check.py`, and
  `ops/healthcheck.py`. These files assemble configuration and start the
  application or operational commands. They do not define business rules.
* **View layer**: `ui/views.py`. Streamlit rendering, forms, status messages,
  and user interaction live here. Views call the UI runtime gateway instead of
  accessing the database directly.
* **UI runtime assembly layer**: `ui/runtime.py`. This is the boundary that
  creates `ToolCallContext`, dispatchers, the model adapter, the AgentLoop,
  and Streamlit error presentation. It does not implement database rules.
* **Harness runtime layer**: `harness/agent_loop.py`, `harness/context.py`,
  `harness/hooks.py`, `harness/security.py`, `harness/tool_protocol.py`, and
  `harness/tool_registry.py`. This layer owns model-to-tool execution,
  validation, permissions, budgets, cancellation, hooks, and standard errors.
* **Tool definition layer**: `harness/tool_models.py` contains model-visible
  Pydantic schemas. `harness/todo_tools.py` contains business-tool handlers
  and the registry. The schema extraction keeps validation definitions out of
  registry construction while `harness.todo_tools` continues to re-export
  the original names for compatibility.
* **Service/business layer**: `database/db.py`, `database/m3.py`, and
  `database/m4.py` contain task, occurrence, reminder, recurrence, goal,
  finance, statistics, audit, and timezone business operations. They enforce
  user ownership, state transitions, timestamps, and transaction boundaries.
* **DAO/database layer**: `database/migrations.py`, connection helpers in
  `database/db.py`, `database/maintenance.py`, `database/schema.py`, and
  `database/checkpointer.py` own SQLite connections, migrations, backup,
  restore, reconciliation, and archived checkpoint access. No table or SQL
  condition was changed by the framework cleanup.
* **Model/state definitions**: `harness/messages.py`, `harness/tool_protocol.py`,
  `harness/context.py`, and the input schemas in `harness/tool_models.py`.
* **Constants/configuration/operations**: `ops/runtime.py` owns environment
  configuration; `ops/observability.py` and `ops/metrics.py` own logging and
  metrics; `harness/instructions.py` remains the model instruction boundary.

## Compatibility retained during cleanup

The archived `legacy/`, `graph/`, `tools/`, and `prompts/` paths remain
unchanged. Existing imports from `harness.todo_tools` also continue to work;
the input classes are imported from `harness.tool_models` internally but are
not renamed or redefined. Database table names, migrations, SQL conditions,
tool names, input schemas, error codes, permission levels, and user-scoping
rules are intentionally retained.
