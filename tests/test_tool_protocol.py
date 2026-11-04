import unittest

from pydantic import BaseModel, ConfigDict, ValidationError

from harness.tool_protocol import (
    PermissionLevel,
    RetryPolicy,
    ToolCallContext,
    ToolContract,
    ToolExecutionError,
    ToolResult,
    ToolSpec,
)


class AddInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str


class AddOutput(BaseModel):
    task_id: int
    title: str


class ToolProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = ToolCallContext(
            user_id="user-1",
            request_id="request-1",
            session_key="session-1",
        )
        self.spec = ToolSpec(
            name="add_task",
            description="Create a task for the current user.",
            input_model=AddInput,
            output_model=AddOutput,
            permission_level=PermissionLevel.WRITE,
            timeout_seconds=10,
            retry_policy=RetryPolicy(
                max_attempts=2,
                backoff_seconds=0.5,
                retryable_error_codes=frozenset({"TEMPORARY_FAILURE"}),
            ),
        )

    def test_spec_exposes_input_and_output_schemas(self) -> None:
        self.assertEqual(self.spec.name, "add_task")
        self.assertEqual(self.spec.permission_level, PermissionLevel.WRITE)
        input_schema = self.spec.input_schema()
        self.assertEqual(input_schema["title"], "AddInput")
        self.assertEqual(input_schema["properties"]["title"]["type"], "string")
        self.assertEqual(input_schema["required"], ["title"])
        self.assertIn("task_id", self.spec.output_schema()["properties"])

    def test_retry_policy_is_declared_in_tool_metadata(self) -> None:
        self.assertEqual(self.spec.retry_policy.max_attempts, 2)
        self.assertEqual(self.spec.retry_policy.backoff_seconds, 0.5)
        self.assertEqual(
            self.spec.retry_policy.retryable_error_codes,
            frozenset({"TEMPORARY_FAILURE"}),
        )

    def test_valid_input_executes_and_returns_structured_output(self) -> None:
        contract = ToolContract(
            self.spec,
            lambda arguments, context: {
                "task_id": 7,
                "title": arguments.title,
            },
        )

        result = contract.execute({"title": "Write docs"}, self.context)

        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"task_id": 7, "title": "Write docs"})
        self.assertEqual(result.request_id, "request-1")
        self.assertIsNone(result.error)

    def test_invalid_input_does_not_call_handler(self) -> None:
        called = False

        def handler(arguments, context):
            nonlocal called
            called = True
            return {"task_id": 1, "title": arguments.title}

        result = ToolContract(self.spec, handler).execute({}, self.context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "INVALID_ARGUMENTS")
        self.assertEqual(
            result.error.message,
            "Tool arguments failed schema validation.",
        )
        self.assertFalse(called)
        self.assertEqual(result.request_id, "request-1")

    def test_extra_input_fields_are_rejected_by_the_input_schema(self) -> None:
        result = ToolContract(
            self.spec,
            lambda arguments, context: {
                "task_id": 1,
                "title": arguments.title,
            },
        ).execute(
            {"title": "Write docs", "unexpected": True},
            self.context,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "INVALID_ARGUMENTS")

    def test_expected_tool_error_is_structured(self) -> None:
        def handler(arguments, context):
            raise ToolExecutionError(
                "TEMPORARY_FAILURE",
                "Storage is temporarily unavailable.",
                retryable=True,
            )

        result = ToolContract(self.spec, handler).execute(
            {"title": "Write docs"},
            self.context,
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "TEMPORARY_FAILURE")
        self.assertEqual(result.error.message, "Storage is temporarily unavailable.")
        self.assertTrue(result.error.retryable)

    def test_invalid_output_is_not_reported_as_success(self) -> None:
        contract = ToolContract(
            self.spec,
            lambda arguments, context: {"title": arguments.title},
        )

        result = contract.execute({"title": "Write docs"}, self.context)

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "INVALID_OUTPUT")
        self.assertEqual(
            result.error.message,
            "Tool returned data that failed its output schema.",
        )

    def test_failure_result_is_valid_when_error_is_present(self) -> None:
        result = ToolResult.failure(
            "TASK_NOT_FOUND",
            "Task does not exist.",
            request_id="request-1",
        )

        self.assertFalse(result.ok)
        self.assertIsNone(result.data)
        self.assertEqual(result.error.code, "TASK_NOT_FOUND")
        self.assertEqual(result.error.message, "Task does not exist.")
        self.assertEqual(result.request_id, "request-1")

    def test_result_envelope_rejects_inconsistent_state(self) -> None:
        with self.assertRaises(ValueError):
            ToolResult(ok=True, error={"code": "BAD", "message": "bad"})

        with self.assertRaises(ValueError):
            ToolResult(ok=False)

    def test_policy_metadata_rejects_invalid_limits(self) -> None:
        with self.assertRaises(ValidationError):
            RetryPolicy(max_attempts=0)

        with self.assertRaises(ValidationError):
            ToolSpec(
                name="read_task",
                description="Read a task.",
                input_model=AddInput,
                timeout_seconds=0,
            )


if __name__ == "__main__":
    unittest.main()
