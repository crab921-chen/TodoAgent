import unittest

from pydantic import BaseModel, ConfigDict

from harness.agent_loop import AgentLoop, LoopBudget, ModelResponse, ToolCall
from harness.hooks import HookSet, RecordingHook, RuntimeEvent, digest_tool_input
from harness.security import ConfirmationResult, PermissionPolicy
from harness.tool_protocol import (
    CancellationToken,
    ErrorCode,
    PermissionLevel,
    ToolCallContext,
    ToolContract,
    ToolSpec,
)
from harness.tool_registry import ToolDispatcher, ToolRegistry


class ActionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)

    def complete(self, messages, tools, cancellation_token):
        return self.responses.pop(0)


class M1RemainingTests(unittest.TestCase):
    def setUp(self):
        self.context = ToolCallContext(
            user_id="user-1",
            request_id="request-m1",
            session_key="session-m1",
        )

    def high_risk_contract(self, calls):
        return ToolContract(
            ToolSpec(
                name="delete_task",
                description="Delete a task.",
                input_model=ActionInput,
                permission_level=PermissionLevel.HIGH_RISK,
            ),
            lambda arguments, context: calls.append(arguments.title) or {"deleted": True},
        )

    def tool_call_model(self):
        return FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="delete-1",
                        name="delete_task",
                        arguments={"title": "Old task"},
                    )
                ]
            ),
            ModelResponse(content="Handled."),
        ])

    def test_high_risk_tool_requires_confirmation_before_handler_execution(self):
        calls = []
        recorder = RecordingHook()
        result = AgentLoop(
            self.tool_call_model(),
            {"delete_task": self.high_risk_contract(calls)},
            hooks=recorder,
        ).run("Delete it.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_results[0].error.code, ErrorCode.CONFIRMATION_REQUIRED)
        self.assertEqual(calls, [])
        confirmation_events = [
            event for event in recorder.events if event.kind == "high_risk_confirm"
        ]
        self.assertEqual(len(confirmation_events), 1)
        self.assertEqual(confirmation_events[0].metadata["decision"], "denied")
        self.assertEqual(
            confirmation_events[0].metadata["reason"],
            "confirmation_callback_missing",
        )
        self.assertEqual(confirmation_events[0].request_id, "request-m1")
        self.assertEqual(confirmation_events[0].user_id, "user-1")
        self.assertEqual(confirmation_events[0].session_key, "session-m1")
        self.assertEqual(confirmation_events[0].tool_name, "delete_task")
        self.assertEqual(confirmation_events[0].tool_call_id, "delete-1")
        self.assertEqual(confirmation_events[0].permission_level, "high_risk")

    def test_confirmed_high_risk_tool_is_allowed(self):
        calls = []
        confirmations = []

        def confirm(context, call, spec):
            confirmations.append((context.request_id, call.id, spec.name))
            return True

        result = AgentLoop(
            self.tool_call_model(),
            {"delete_task": self.high_risk_contract(calls)},
            authorization=PermissionPolicy(confirmation_callback=confirm),
        ).run("Delete it.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(calls, ["Old task"])
        self.assertEqual(confirmations, [("request-m1", "delete-1", "delete_task")])
        self.assertTrue(result.tool_results[0].ok)

    def test_permission_allow_list_blocks_tool_without_calling_handler(self):
        calls = []
        policy = PermissionPolicy(allowed_levels=frozenset({PermissionLevel.READ}))
        result = AgentLoop(
            self.tool_call_model(),
            {"delete_task": self.high_risk_contract(calls)},
            authorization=policy,
        ).run("Delete it.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_results[0].error.code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(calls, [])

    def test_explicit_empty_allow_list_does_not_reset_to_defaults(self):
        policy = PermissionPolicy(allowed_levels=frozenset())
        contract = self.high_risk_contract([])
        result = policy.authorize(
            ToolCall(id="delete-1", name="delete_task", arguments={"title": "Old task"}),
            contract.spec,
            self.context,
        )

        self.assertEqual(result.error.code, ErrorCode.PERMISSION_DENIED)

    def test_policy_can_deny_one_tool_name(self):
        policy = PermissionPolicy(denied_tool_names=frozenset({"delete_task"}))
        contract = self.high_risk_contract([])
        result = policy.authorize(
            ToolCall(id="delete-1", name="delete_task", arguments={"title": "Old task"}),
            contract.spec,
            self.context,
        )

        self.assertEqual(result.error.code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(result.error.details["reason"], "tool_denied")

    def test_confirmation_callback_failure_is_distinct_from_user_denial(self):
        def failing_confirmation(context, call, spec):
            raise RuntimeError("UI unavailable")

        policy = PermissionPolicy(confirmation_callback=failing_confirmation)
        contract = self.high_risk_contract([])
        result = policy.authorize(
            ToolCall(id="delete-1", name="delete_task", arguments={"title": "Old task"}),
            contract.spec,
            self.context,
        )

        self.assertEqual(result.error.code, ErrorCode.CONFIRMATION_FAILED)
        self.assertEqual(
            result.error.details["reason"],
            "confirmation_callback_error",
        )
        self.assertEqual(result.error.details["exception_type"], "RuntimeError")

    def test_confirmation_callback_failure_emits_denied_high_risk_event(self):
        calls = []
        recorder = RecordingHook()

        def failing_confirmation(context, call, spec):
            raise RuntimeError("UI unavailable")

        result = AgentLoop(
            self.tool_call_model(),
            {"delete_task": self.high_risk_contract(calls)},
            authorization=PermissionPolicy(confirmation_callback=failing_confirmation),
            hooks=recorder,
        ).run("Delete it.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_results[0].error.code, ErrorCode.CONFIRMATION_FAILED)
        self.assertEqual(calls, [])
        event = [item for item in recorder.events if item.kind == "high_risk_confirm"][0]
        self.assertEqual(event.metadata["decision"], "denied")
        self.assertEqual(event.error_code, ErrorCode.CONFIRMATION_FAILED)

    def test_user_confirmation_rejection_emits_denied_high_risk_event(self):
        recorder = RecordingHook()
        result = AgentLoop(
            self.tool_call_model(),
            {"delete_task": self.high_risk_contract([])},
            authorization=PermissionPolicy(confirmation_callback=lambda context, call, spec: False),
            hooks=recorder,
        ).run("Delete it.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_results[0].error.code, ErrorCode.CONFIRMATION_DENIED)
        event = [item for item in recorder.events if item.kind == "high_risk_confirm"][0]
        self.assertEqual(event.metadata["decision"], "denied")
        self.assertEqual(event.error_code, ErrorCode.CONFIRMATION_DENIED)

    def test_confirmation_result_can_explain_user_denial(self):
        policy = PermissionPolicy(
            confirmation_callback=lambda context, call, spec: ConfirmationResult(
                confirmed=False,
                reason="user_cancelled",
            )
        )
        contract = self.high_risk_contract([])
        result = policy.authorize(
            ToolCall(id="delete-1", name="delete_task", arguments={"title": "Old task"}),
            contract.spec,
            self.context,
        )

        self.assertEqual(result.error.code, ErrorCode.CONFIRMATION_DENIED)
        self.assertEqual(result.error.details["reason"], "user_cancelled")

    def test_dispatcher_default_policy_blocks_high_risk_tool(self):
        calls = []
        result = ToolDispatcher(
            ToolRegistry([self.high_risk_contract(calls)])
        ).dispatch(
            "delete_task",
            {"title": "Old task"},
            self.context,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.CONFIRMATION_REQUIRED)
        self.assertEqual(calls, [])

    def test_model_and_tool_hooks_are_request_correlated(self):
        calls = []
        recorder = RecordingHook()
        contract = ToolContract(
            ToolSpec(
                name="read_task",
                description="Read a task.",
                input_model=ActionInput,
            ),
            lambda arguments, context: calls.append(arguments.title) or {"title": arguments.title},
        )
        result = AgentLoop(
            self.tool_call_model_for("read_task"),
            {"read_task": contract},
            hooks=recorder,
        ).run("Read it.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(calls, ["Old task"])
        self.assertEqual(
            [(event.kind, event.phase) for event in recorder.events],
            [
                ("model", "before"),
                ("model", "after"),
                ("tool", "before"),
                ("tool", "after"),
                ("model", "before"),
                ("model", "after"),
            ],
        )
        self.assertTrue(all(event.request_id == "request-m1" for event in recorder.events))
        tool_after = [
            event for event in recorder.events
            if event.kind == "tool" and event.phase == "after"
        ][0]
        self.assertEqual(tool_after.tool_call_id, "read-1")
        self.assertEqual(tool_after.user_id, "user-1")
        self.assertEqual(tool_after.session_key, "session-m1")
        self.assertEqual(len(tool_after.tool_input_digest), 64)
        self.assertNotIn("Old task", str(tool_after.metadata))
        self.assertTrue(tool_after.ok)
        self.assertIsNotNone(tool_after.duration_seconds)

    def test_runtime_event_bounds_metadata_and_validates_duration(self):
        source_metadata = {
            "safe": "x" * 600,
            "nested": {"secret": "do not retain"},
        }
        event = RuntimeEvent(
            kind="tool",
            phase="after",
            request_id="request-m1",
            user_id="user-1",
            metadata=source_metadata,
        )

        self.assertEqual(len(event.metadata["safe"]), 500)
        self.assertEqual(event.metadata["nested"], "<redacted>")
        self.assertEqual(source_metadata["safe"], "x" * 600)
        self.assertEqual(source_metadata["nested"], {"secret": "do not retain"})
        with self.assertRaises(ValueError):
            RuntimeEvent(
                kind="tool",
                phase="after",
                request_id="request-m1",
                user_id="user-1",
                duration_seconds=-0.1,
            )
        with self.assertRaises(ValueError):
            RuntimeEvent(
                kind="not_a_runtime_event",
                phase="occurred",
                request_id="request-m1",
                user_id="user-1",
            )
        with self.assertRaises(ValueError):
            RuntimeEvent(
                kind="tool",
                phase="invalid_phase",
                request_id="request-m1",
                user_id="user-1",
            )

    def test_tool_input_digest_is_stable_without_storing_arguments(self):
        first = digest_tool_input({"title": "A", "count": 1})
        second = digest_tool_input({"count": 1, "title": "A"})

        self.assertEqual(first, second)
        self.assertEqual(len(first), 64)
        special = digest_tool_input({"text": "中文\n{}\"", "items": ["x", 1]})
        self.assertEqual(special, digest_tool_input({"items": ["x", 1], "text": "中文\n{}\""}))
        self.assertNotIn("中文", special)

    def test_hook_set_supports_runtime_registration_and_recording_reset(self):
        recorder = RecordingHook()
        hooks = HookSet()
        event = RuntimeEvent(
            kind="cancellation",
            phase="occurred",
            request_id="request-m1",
            user_id="user-1",
        )

        hooks.register(recorder)
        hooks.emit(event)
        self.assertEqual(len(recorder.events), 1)
        self.assertTrue(hooks.unregister(recorder))
        hooks.emit(event)
        self.assertEqual(len(recorder.events), 1)
        recorder.clear()
        self.assertEqual(recorder.events, ())

    def test_hook_set_isolates_one_hook_failure_from_other_hooks(self):
        class FailingHook:
            def emit(self, event):
                raise RuntimeError("hook failure")

        recorder = RecordingHook()
        hooks = HookSet([FailingHook(), recorder])
        with self.assertLogs("harness.hooks", level="ERROR") as logs:
            hooks.emit(
                RuntimeEvent(
                    kind="tool",
                    phase="after",
                    request_id="request-m1",
                    user_id="user-1",
                )
            )

        self.assertEqual(len(recorder.events), 1)
        self.assertIn("Runtime hook failed", logs.output[0])

    def test_budget_exceeded_emits_budget_event(self):
        recorder = RecordingHook()
        model = FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(id="one", name="read_task", arguments={"title": "A"}),
                    ToolCall(id="two", name="read_task", arguments={"title": "B"}),
                ]
            )
        ])
        contract = ToolContract(
            ToolSpec(name="read_task", description="Read a task.", input_model=ActionInput),
            lambda arguments, context: {"title": arguments.title},
        )

        result = AgentLoop(
            model,
            {"read_task": contract},
            budget=LoopBudget(
                max_tool_calls=1,
            ),
            hooks=recorder,
        ).run("Read two.", self.context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.TOOL_CALL_LIMIT_REACHED)
        budget_events = [item for item in recorder.events if item.kind == "budget_exceeded"]
        self.assertEqual(len(budget_events), 1)
        self.assertEqual(budget_events[0].error_code, ErrorCode.TOOL_CALL_LIMIT_REACHED)

    def test_cancellation_emits_cancellation_event(self):
        token = CancellationToken()
        token.cancel()
        recorder = RecordingHook()

        result = AgentLoop(FakeModel([]), {}, hooks=recorder).run(
            "Cancel.",
            self.context,
            cancellation_token=token,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.CANCELLED)
        cancellation_events = [item for item in recorder.events if item.kind == "cancellation"]
        self.assertEqual(len(cancellation_events), 1)
        self.assertEqual(cancellation_events[0].error_code, ErrorCode.CANCELLED)

    def test_invalid_permission_level_is_denied_by_policy(self):
        policy = PermissionPolicy()
        contract = self.high_risk_contract([])
        contract.spec.permission_level = "not_a_permission_level"

        result = policy.authorize(
            ToolCall(id="delete-1", name="delete_task", arguments={"title": "Old task"}),
            contract.spec,
            self.context,
        )

        self.assertEqual(result.error.code, ErrorCode.PERMISSION_DENIED)
        self.assertEqual(result.error.details["reason"], "invalid_permission_level")

    @staticmethod
    def tool_call_model_for(name):
        return FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="read-1",
                        name=name,
                        arguments={"title": "Old task"},
                    )
                ]
            ),
            ModelResponse(content="Handled."),
        ])


if __name__ == "__main__":
    unittest.main()
