# pyright: ignore
import json
import os
from pathlib import Path
from typing import cast

from dotenv import load_dotenv
from openai import OpenAI
from openai.types.chat import ChatCompletionToolParam

from learncode_agent.tools import (
    add_nums,
    edit_file,
    make_tool_schema,
    read_file,
    run_bash_command,
    subtract_nums,
)


def load_system_prompt(path: Path) -> str:
    return "Be helpful. Use tools when you necessary"
    if not path.is_file():
        raise FileNotFoundError(f"System prompt file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def main() -> int:
    load_dotenv(Path.cwd() / ".env")
    client = OpenAI()
    model = os.getenv("LEARNCODE_MODEL", "gpt-4.1-mini")
    system_prompt = load_system_prompt(Path("prompts/brainstorm_system_prompt.md"))
    messages: list = [{"role": "system", "content": system_prompt}]

    TOOL_FUNCTIONS = {
        "run_bash_command": run_bash_command,
        "add_nums": add_nums,
        "subtract_nums": subtract_nums,
        "read_file": read_file,
        "edit_file": edit_file,
    }

    TOOLS = [make_tool_schema(f) for f in TOOL_FUNCTIONS.values()]

    while True:
        try:
            user_text = input("> ").strip()
            print("\n")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            return 0
        if not user_text:
            continue
        if user_text in ("quit", "q"):
            print("Goodbye!")
            return 0

        messages.append({"role": "user", "content": user_text})

        while True:
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            message = response.choices[0].message
            messages.append(message)

            if not message.tool_calls:
                print(f"Assistant: {message.content}\n")
                break

            for tool_call in message.tool_calls:
                func = TOOL_FUNCTIONS[tool_call.function.name]
                kwargs = json.loads(tool_call.function.arguments)
                print(f"[Tool call] {tool_call.function.name}({json.dumps(kwargs, indent=2)})\n")
                result = func(**kwargs)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": str(result),
                })


if __name__ == "__main__":
    raise SystemExit(main())
