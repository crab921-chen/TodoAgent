# Unified Framework Migration

## Classification

### Retained business logic

* `database/`: versioned migrations and M2-M4 task, occurrence, reminder,
  recurrence, goal, finance, statistics, session, audit, backup, and recovery
  services.
* `harness/todo_tools.py`: the framework-neutral contracts for all retained
  business capabilities.
* `harness/tool_protocol.py`, `harness/tool_registry.py`, and
  `harness/security.py`: validation, dispatch, permission, confirmation,
  timeout, retry, and cancellation boundaries.
* `ops/`: configuration, startup, health, logging, redaction, and metrics.

### Deprecated legacy transport and UI

* `legacy/`: the original LangGraph/LangChain graph, prompt, and tool
  implementation. It is retained for compatibility and comparison only.
* `graph/`, `tools/add_task.py`, `tools/view_tasks.py`, and the other historical
  task modules: import-compatible shims into `legacy/`.
* `database/checkpointer.py` and `tests/legacy/`: checkpoint support and
  diagnostic scripts needed by the archived implementation.

These files are not imported by the public application or CLI entrypoints.

### Unified framework

* `harness/agent_loop.py`: the public model-to-tool execution loop.
* `harness/groq_model.py`: the Groq-to-Harness model adapter.
* `harness/instructions.py`: the shared policy instruction for the model.
* `app.py`: the only Streamlit page and menu system.
* `cli.py`: the terminal entrypoint using the same AgentLoop and tool registry.

## Completed migration tasks

1. Replaced the Streamlit LangGraph conversation path with `AgentLoop`.
2. Replaced the CLI LangGraph conversation path with `AgentLoop`.
3. Exposed all M1-M5 business operations through the framework-neutral tool
   registry, including reminders, recurrence, goals, finance, links,
   statistics, timezone, audit, and health operations.
4. Kept authenticated user identity in `ToolCallContext`; it is never a model
   argument.
5. Kept high-risk confirmation in `PermissionPolicy` for both structured UI
   actions and natural-language tool calls.
6. Removed legacy pages, sidebars, route calls, and old UI components from the
   public Streamlit entrypoint.
7. Kept compatibility shims and archived diagnostics without routing new work
   through them.

## Migration risks and test coverage

* User isolation: every public tool receives the trusted user from
  `ToolCallContext`; cross-user CRUD and finance/link access are covered by M2,
  M3, and M4 tests.
* High-risk writes: archive, void, and explicit links must remain blocked until
  confirmation; permission and delivery tests cover denial and confirmation.
* Idempotency: completion, reminder delivery, and finance writes must not be
  duplicated during model retries; M2-M5 data tests cover these boundaries.
* Timezones: naive timestamps must use the user's configured timezone and
  stored values must remain UTC; M2-M4 tests cover DST and local-day bounds.
* Context growth: the AgentLoop bounds history and tool results before every
  model call; AgentLoop tests cover message limits, cancellation, and budgets.
* Operational safety: secrets, raw arguments, and prompts must not enter logs
  or audit rows; M5 operations and delivery tests cover redaction and release
  gates.
* Legacy dependencies: the archived graph still needs its optional LangGraph
  dependencies when someone explicitly runs an archived diagnostic. This does
  not affect the public Harness runtime.

## Verification gate

The migration is accepted only when all of the following pass:

```text
python tools/ci_checks.py --release
python -m compileall -q app.py cli.py database graph harness legacy ops prompts tests tools
python -m unittest tests.test_tool_protocol tests.test_agent_loop tests.test_unified_runtime tests.test_m1_remaining tests.test_m2_data tests.test_m3_data tests.test_m4_data tests.test_m5_delivery tests.test_m5_operations -q
python tools/release_signoff.py
```
