import unittest

from harness.groq_model import GroqModelInvoker
from harness.messages import LoopMessage
from harness.tool_protocol import CancellationToken, ToolSpec
from pydantic import BaseModel


class EmptyInput(BaseModel):
    pass


class FakeFunction:
    name = "todo_list_tasks"
    arguments = '{"limit": 10}'


class FakeToolCall:
    id = "call-1"
    function = FakeFunction()


class FakeMessage:
    content = "Here are your tasks."
    tool_calls = [FakeToolCall()]


class FakeChoice:
    message = FakeMessage()


class FakeUsage:
    total_tokens = 12


class FakeCompletion:
    choices = [FakeChoice()]
    usage = FakeUsage()


class FakeCompletions:
    def create(self, **kwargs):
        self.kwargs = kwargs
        return FakeCompletion()


class FakeChat:
    def __init__(self):
        self.completions = FakeCompletions()


class FakeClient:
    def __init__(self):
        self.chat = FakeChat()


class UnifiedRuntimeTests(unittest.TestCase):
    def test_groq_adapter_translates_harness_messages_and_tools(self):
        client = FakeClient()
        model = GroqModelInvoker(
            "test-key",
            model_name="test-model",
            client=client,
        )
        spec = ToolSpec(
            name="todo_list_tasks",
            description="List tasks.",
            input_model=EmptyInput,
        )

        response = model.complete(
            [
                LoopMessage(role="system", content="Use tools."),
                LoopMessage(role="user", content="Show tasks."),
            ],
            [spec],
            CancellationToken(),
        )

        self.assertEqual(response.content, "Here are your tasks.")
        self.assertEqual(response.tool_calls[0].name, "todo_list_tasks")
        self.assertEqual(response.tool_calls[0].arguments, {"limit": 10})
        self.assertEqual(response.total_tokens, 12)
        request = client.chat.completions.kwargs
        self.assertEqual(request["model"], "test-model")
        self.assertEqual(request["tools"][0]["function"]["name"], "todo_list_tasks")
        self.assertEqual(request["messages"][1]["role"], "user")


if __name__ == "__main__":
    unittest.main()
