"""Terminal entrypoint for the unified Harness-based Todo Agent."""

from __future__ import annotations

from uuid import uuid4

from database.schema import initialize_database
from harness.agent_loop import AgentLoop, LoopBudget
from harness.openai_compatible_model import OpenAICompatibleModelInvoker
from harness.instructions import SYSTEM_INSTRUCTION
from harness.security import PermissionPolicy
from harness.tool_protocol import ToolCallContext
from harness.todo_tools import build_todo_tool_registry
from ops.observability import configure_logging
from ops.runtime import ConfigurationError, load_config


def main() -> int:
    try:
        config = load_config()
        config.ensure_directories()
        initialize_database(config.database_path)
    except (ConfigurationError, OSError, RuntimeError) as error:
        print(f"Startup failed: {type(error).__name__}: {error}")
        return 1
    if not config.groq_api_key:
        print("ZHIPU_API_KEY is not configured; set it in the environment before starting the CLI.")
        return 1

    configure_logging(config.log_level)
    user_id = input("User name [aman]: ").strip() or "aman"
    session_key = f"cli:{user_id}"
    history = []

    def confirm(context, call, spec) -> bool:
        answer = input(f"Confirm high-risk action '{spec.name}'? [y/N] ").strip().lower()
        return answer in {"y", "yes"}

    loop = AgentLoop(
        OpenAICompatibleModelInvoker(
            config.groq_api_key,
            model_name=config.model_name,
            base_url=config.model_base_url,
            temperature=config.model_temperature,
        ),
        build_todo_tool_registry(),
        system_instruction=SYSTEM_INSTRUCTION,
        budget=LoopBudget(
            max_tool_calls=config.max_tool_calls,
            model_timeout_seconds=config.model_timeout_seconds,
        ),
        authorization=PermissionPolicy(confirmation_callback=confirm),
    )
    print("Todo Agent started. Type 'exit' or 'quit' to stop.")
    while True:
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            print("Todo Agent stopped.")
            return 0
        context = ToolCallContext(
            user_id=user_id,
            request_id=f"cli:{uuid4().hex}",
            session_key=session_key,
        )
        result = loop.run(user_input, context, history=history)
        if result.ok:
            history = list(result.messages[1:])
            print(f"\nAssistant: {result.final_response or '已完成。'}")
        else:
            print(f"\nAssistant error: {result.error.code}: {result.error.message}")


if __name__ == "__main__":
    raise SystemExit(main())
