import os
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

def load_system_prompt(path: Path) -> str:
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

    while True:
        try:
            user_text = input("> ").strip()
            print("\n")
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            return 0
        if not user_text:
            continue

        messages.append({"role": "user", "content": user_text})
        response = client.responses.create(model=model, input=messages)
        reply = response.output_text
        print(f"Assistant: {reply}\n")
        messages.append({"role": "assistant", "content": reply})


if __name__ == "__main__":
    raise SystemExit(main())
