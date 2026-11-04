import unittest
from dataclasses import replace
from threading import Event, Thread

from pydantic import BaseModel

from harness.agent_loop import AgentLoop, LoopBudget, LoopMessage, ModelResponse, ToolCall
from harness.context import ContextAssembler, ContextLimits
from harness.tool_protocol import (
    CancellationToken,
    ErrorCode,
    ToolCallContext,
    ToolContract,
    ToolExecutionError,
    ToolSpec,
)
from harness.tool_registry import ToolDispatcher, ToolRegistry


class AddInput(BaseModel):
    title: str


class FakeModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, tools, cancellation_token):
        self.calls.append((messages, tools))
        return self.responses.pop(0)


def add_contract(calls):
    return ToolContract(
        ToolSpec(
            name="add_task",
            description="Create a task.",
            input_model=AddInput,
        ),
        lambda arguments, context: calls.append(
            (context.user_id, arguments.title)
        ) or {"title": arguments.title},
    )


def failing_contract():
    def fail(arguments, context):
        raise ToolExecutionError("EXPECTED_FAILURE", "Tool failed as expected.")

    return ToolContract(
        ToolSpec(
            name="fail_task",
            description="Return a controlled failure.",
            input_model=AddInput,
        ),
        fail,
    )


class AgentLoopTests(unittest.TestCase):
    def setUp(self):
        self.context = ToolCallContext(
            user_id="user-1",
            request_id="request-1",
        )

    def test_plain_response_ends_without_tool_execution(self):
        """A response without tool calls ends after one model turn."""
        model = FakeModel([ModelResponse(content="Hello")])
        loop = AgentLoop(model, {})

        result = loop.run("Hello", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.final_response, "Hello")
        self.assertEqual(result.tool_call_count, 0)
        self.assertEqual(len(model.calls), 1)

    def test_single_tool_call_returns_result_to_model(self):
        """A tool result is appended before the final model turn."""
        calls = []
        model = FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="add_task",
                        arguments={"title": "Write docs"},
                    )
                ]
            ),
            ModelResponse(content="Task created."),
        ])
        loop = AgentLoop(model, {"add_task": add_contract(calls)})

        result = loop.run("Add a task to write docs.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.final_response, "Task created.")
        self.assertEqual(result.tool_call_count, 1)
        self.assertEqual(calls, [("user-1", "Write docs")])
        self.assertEqual(len(model.calls), 2)
        tool_message = result.messages[-2]
        self.assertIsInstance(tool_message, LoopMessage)
        self.assertEqual(tool_message.role, "tool")
        self.assertEqual(tool_message.tool_call_id, "call-1")
        self.assertTrue(tool_message.content["ok"])

    def test_multiple_tool_rounds_continue_until_final_response(self):
        """Multiple model-tool rounds continue until the model finishes."""
        calls = []
        model = FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="add_task",
                        arguments={"title": "First"},
                    )
                ]
            ),
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-2",
                        name="add_task",
                        arguments={"title": "Second"},
                    )
                ]
            ),
            ModelResponse(content="Both tasks created."),
        ])
        loop = AgentLoop(model, {"add_task": add_contract(calls)})

        result = loop.run("Create two tasks.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.final_response, "Both tasks created.")
        self.assertEqual(result.tool_call_count, 2)
        self.assertEqual(calls, [("user-1", "First"), ("user-1", "Second")])
        self.assertEqual(len(model.calls), 3)

    def test_unknown_tool_is_returned_to_model_as_structured_failure(self):
        """An unknown tool becomes a structured failure, not a crash."""
        model = FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="call-1",
                        name="missing_tool",
                        arguments={},
                    )
                ]
            ),
            ModelResponse(content="I could not run that tool."),
        ])
        loop = AgentLoop(model, {})

        result = loop.run("Do it.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.tool_results[0].error.code, ErrorCode.UNKNOWN_TOOL)
        self.assertEqual(result.final_response, "I could not run that tool.")

    def test_model_failure_is_returned_as_loop_error(self):
        """A model adapter exception terminates the loop with an error code."""
        class FailingModel:
            def complete(self, messages, tools, cancellation_token):
                raise RuntimeError("provider unavailable")

        result = AgentLoop(FailingModel(), {}).run("Hello", self.context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.MODEL_EXECUTION_FAILED)

    def test_tool_call_budget_stops_before_partial_batch_execution(self):
        """A batch over budget is rejected before any tool in the batch runs."""
        calls = []
        model = FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(id="call-1", name="add_task", arguments={"title": "A"}),
                    ToolCall(id="call-2", name="add_task", arguments={"title": "B"}),
                ]
            )
        ])
        loop = AgentLoop(
            model,
            {"add_task": add_contract(calls)},
            budget=LoopBudget(max_tool_calls=1),
        )

        result = loop.run("Add two.", self.context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.TOOL_CALL_LIMIT_REACHED)
        self.assertEqual(calls, [])

    def test_model_call_budget_stops_repeated_tool_cycles(self):
        """Repeated tool cycles stop when the model-call budget is exhausted."""
        calls = []
        repeated = ModelResponse(
            tool_calls=[ToolCall(id="call-1", name="add_task", arguments={"title": "A"})]
        )
        model = FakeModel([repeated, repeated])
        loop = AgentLoop(
            model,
            {"add_task": add_contract(calls)},
            budget=LoopBudget(max_model_calls=2),
        )

        result = loop.run("Keep adding.", self.context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.MODEL_CALL_LIMIT_REACHED)
        self.assertEqual(result.tool_call_count, 2)

    def test_model_token_budget_stops_when_reported_usage_exceeds_limit(self):
        """Reported model usage cannot exceed the configured token budget."""
        model = FakeModel([ModelResponse(content="Answer", total_tokens=11)])
        loop = AgentLoop(
            model,
            {},
            budget=LoopBudget(max_model_tokens=10),
        )

        result = loop.run("Use tokens.", self.context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.MODEL_TOKEN_BUDGET_EXCEEDED)

    def test_total_time_budget_limits_model_wait(self):
        """The total request budget limits a slow model invocation."""
        started = Event()
        release = Event()

        class SlowModel:
            def complete(self, messages, tools, cancellation_token):
                started.set()
                release.wait()
                return ModelResponse(content="late")

        try:
            result = AgentLoop(
                SlowModel(),
                {},
                budget=LoopBudget(max_total_seconds=0.02, model_timeout_seconds=1),
            ).run("Wait.", self.context)
        finally:
            release.set()

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.TOTAL_TIME_LIMIT_REACHED)

    def test_model_timeout_returns_without_waiting_for_slow_adapter(self):
        """A model invocation has its own timeout independent of total budget."""
        started = Event()
        release = Event()

        class SlowModel:
            def complete(self, messages, tools, cancellation_token):
                started.set()
                release.wait()
                return ModelResponse(content="late")

        loop = AgentLoop(
            SlowModel(),
            {},
            budget=LoopBudget(model_timeout_seconds=0.01, max_total_seconds=1),
        )

        try:
            result = loop.run("Wait.", self.context)
        finally:
            release.set()

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.MODEL_TIMEOUT)

    def test_cancelled_request_does_not_call_model(self):
        """A request cancelled before execution never calls the model."""
        class MustNotRunModel:
            def complete(self, messages, tools, cancellation_token):
                raise AssertionError("model should not be called")

        token = CancellationToken()
        token.cancel()
        result = AgentLoop(MustNotRunModel(), {}).run(
            "Cancel this.",
            self.context,
            cancellation_token=token,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.CANCELLED)

    def test_cancelled_model_wait_returns_cancelled_result(self):
        """A cooperative model wait observes request cancellation."""
        started = Event()

        class CooperativeModel:
            def complete(self, messages, tools, cancellation_token):
                started.set()
                cancellation_token.wait()
                return ModelResponse(content="cancelled")

        token = CancellationToken()
        result_holder = []
        runner = Thread(
            target=lambda: result_holder.append(
                AgentLoop(CooperativeModel(), {}).run(
                    "Cancel while waiting.",
                    self.context,
                    cancellation_token=token,
                )
            )
        )
        runner.start()
        self.assertTrue(started.wait(1))
        token.cancel()
        runner.join(1)

        self.assertFalse(runner.is_alive())
        self.assertEqual(result_holder[0].error.code, ErrorCode.CANCELLED)

    def test_tool_dispatcher_enforces_spec_timeout(self):
        """The dispatcher returns a timeout result for a slow tool."""
        started = Event()
        release = Event()

        def slow_tool(arguments, context):
            started.set()
            release.wait()

        contract = ToolContract(
            ToolSpec(name="slow", description="Slow tool.", input_model=AddInput, timeout_seconds=0.01),
            slow_tool,
        )

        try:
            result = ToolDispatcher(ToolRegistry([contract])).dispatch(
                "slow",
                {"title": "Wait"},
                self.context,
            )
        finally:
            release.set()

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.TOOL_TIMEOUT)

    def test_tool_dispatcher_observes_cancellation(self):
        """A cooperative tool wait observes request cancellation."""
        started = Event()

        def cooperative_tool(arguments, context):
            started.set()
            context.cancellation_token.wait()
            return {"title": arguments.title}

        contract = ToolContract(
            ToolSpec(name="wait", description="Wait for cancellation.", input_model=AddInput),
            cooperative_tool,
        )
        token = CancellationToken()
        result_holder = []
        runner = Thread(
            target=lambda: result_holder.append(
                ToolDispatcher(ToolRegistry([contract])).dispatch(
                    "wait",
                    {"title": "Wait"},
                    replace(self.context, cancellation_token=token),
                    timeout_seconds=1,
                )
            )
        )
        runner.start()
        self.assertTrue(started.wait(1))
        token.cancel()
        runner.join(1)

        self.assertFalse(runner.is_alive())
        self.assertEqual(result_holder[0].error.code, ErrorCode.CANCELLED)

    def test_handler_exception_is_structured_error(self):
        """A regular handler exception is normalized by the tool contract."""
        contract = ToolContract(
            ToolSpec(name="broken", description="Broken tool.", input_model=AddInput),
            lambda arguments, context: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        result = ToolDispatcher(ToolRegistry([contract])).dispatch(
            "broken",
            {"title": "Test"},
            self.context,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, ErrorCode.TOOL_EXECUTION_FAILED)

    def test_one_batch_can_return_both_success_and_failure_results(self):
        """Each tool result in a mixed batch is returned to the model."""
        calls = []
        model = FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(id="success", name="add_task", arguments={"title": "A"}),
                    ToolCall(id="failure", name="fail_task", arguments={"title": "B"}),
                ]
            ),
            ModelResponse(content="One succeeded and one failed."),
        ])
        loop = AgentLoop(
            model,
            {
                "add_task": add_contract(calls),
                "fail_task": failing_contract(),
            },
        )

        result = loop.run("Run both.", self.context)

        self.assertTrue(result.ok)
        self.assertEqual([item.ok for item in result.tool_results], [True, False])
        self.assertEqual(result.tool_results[1].error.code, "EXPECTED_FAILURE")
        tool_messages = [message for message in model.calls[1][0] if message.role == "tool"]
        self.assertEqual(
            [message.tool_call_id for message in tool_messages],
            ["success", "failure"],
        )


class ToolRegistryTests(unittest.TestCase):
    def test_registry_rejects_duplicate_and_exposes_specs(self):
        """Registration order is stable and duplicate names are rejected."""
        contract = add_contract([])
        registry = ToolRegistry([contract])

        with self.assertRaises(ValueError):
            registry.register(contract)

        self.assertEqual([spec.name for spec in registry.specs()], ["add_task"])


class ContextAssemblerTests(unittest.TestCase):
    def test_assembly_orders_system_history_current_input_and_runtime(self):
        """Context order is system, history, current input, then runtime turns."""
        assembler = ContextAssembler("System rule")
        history = [LoopMessage(role="assistant", content="Earlier")]
        tool_result = LoopMessage(role="tool", content={"ok": True})

        messages = assembler.assemble(
            current_input="Current",
            history=history,
            runtime_messages=[tool_result],
        )

        self.assertEqual([message.role for message in messages], ["system", "assistant", "user", "tool"])
        self.assertEqual(messages[0].content, "System rule")
        self.assertEqual(messages[2].content, "Current")

    def test_context_trims_old_history_and_caps_message_sizes(self):
        """Old history and oversized input are bounded by context limits."""
        assembler = ContextAssembler(
            "Policy",
            limits=ContextLimits(
                max_history_messages=2,
                max_message_chars=20,
                max_tool_result_chars=30,
                max_total_chars=100,
            ),
        )
        history = [
            LoopMessage(role="user", content="old-1"),
            LoopMessage(role="assistant", content="old-2"),
            LoopMessage(role="assistant", content="keep-this"),
        ]

        messages = assembler.assemble(current_input="x" * 100, history=history)

        self.assertIn("keep-this", [message.content for message in messages])
        self.assertTrue(messages[-1].content.endswith("...[truncated]"))
        self.assertLessEqual(assembler.total_size(messages), 100)

    def test_oversized_tool_result_is_summarized(self):
        """Oversized structured tool output is replaced with a bounded preview."""
        assembler = ContextAssembler(
            "Policy",
            limits=ContextLimits(max_tool_result_chars=40, max_total_chars=500),
        )
        messages = assembler.assemble(
            current_input="question",
            runtime_messages=[LoopMessage(role="tool", content={"payload": "x" * 200})],
        )

        result = messages[-1].content
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["preview"]), 40)

    def test_system_instruction_is_preserved_when_history_is_trimmed(self):
        """System instructions remain while lower-priority history is removed."""
        system_instruction = "System policy must remain intact."
        assembler = ContextAssembler(
            system_instruction,
            limits=ContextLimits(
                max_message_chars=1000,
                max_total_chars=120,
            ),
        )

        messages = assembler.assemble(
            current_input="Current request",
            history=[LoopMessage(role="assistant", content="old" * 100)],
        )

        self.assertEqual(messages[0].role, "system")
        self.assertEqual(messages[0].content, system_instruction)

    def test_truncated_tool_result_is_sent_to_the_next_model_turn(self):
        """A summarized tool result, rather than the full payload, reaches the model."""
        model = FakeModel([
            ModelResponse(
                tool_calls=[
                    ToolCall(
                        id="large-output",
                        name="large_output",
                        arguments={"title": "Generate"},
                    )
                ]
            ),
            ModelResponse(content="Done."),
        ])
        contract = ToolContract(
            ToolSpec(
                name="large_output",
                description="Return a large result.",
                input_model=AddInput,
            ),
            lambda arguments, context: {"payload": "x" * 500},
        )
        loop = AgentLoop(
            model,
            {"large_output": contract},
            context_assembler=ContextAssembler(
                "Policy",
                limits=ContextLimits(
                    max_tool_result_chars=40,
                    max_total_chars=500,
                ),
            ),
        )

        result = loop.run(
            "Generate a large result.",
            ToolCallContext(user_id="user-1", request_id="request-1"),
        )

        self.assertTrue(result.ok)
        tool_messages = [message for message in model.calls[1][0] if message.role == "tool"]
        self.assertEqual(len(tool_messages), 1)
        self.assertTrue(tool_messages[0].content["truncated"])

    def test_context_trimming_keeps_tool_calls_paired_with_results(self):
        """Trimming preserves call/result order and removes complete old turns."""
        assembler = ContextAssembler(
            "Policy",
            limits=ContextLimits(
                max_message_chars=100,
                max_tool_result_chars=100,
                max_total_chars=220,
            ),
        )
        old_call = ToolCall(id="old-call", name="add_task", arguments={"title": "old"})
        new_call = ToolCall(id="new-call", name="add_task", arguments={"title": "new"})
        history = [
            LoopMessage(role="assistant", content=None, tool_calls=[old_call]),
            LoopMessage(role="tool", content={"payload": "old" * 20}, tool_call_id="old-call"),
            LoopMessage(role="assistant", content=None, tool_calls=[new_call]),
            LoopMessage(role="tool", content={"payload": "new" * 20}, tool_call_id="new-call"),
        ]

        messages = assembler.assemble(current_input="continue", history=history)

        call_ids = [
            call.id
            for message in messages
            if message.role == "assistant"
            for call in message.tool_calls
        ]
        result_ids = [
            message.tool_call_id
            for message in messages
            if message.role == "tool"
        ]
        self.assertEqual(call_ids, result_ids)

        assistant_positions = {
            call.id: index
            for index, message in enumerate(messages)
            if message.role == "assistant"
            for call in message.tool_calls
        }
        for index, message in enumerate(messages):
            if message.role == "tool":
                self.assertLess(assistant_positions[message.tool_call_id], index)


if __name__ == "__main__":
    unittest.main()
