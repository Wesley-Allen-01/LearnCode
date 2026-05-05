from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from dotenv import load_dotenv
from prompt_toolkit.formatted_text import ANSI

from learncode_agent.terminal_style import (
    BG_DARK,
    BOLD,
    CYAN,
    DIM,
    MAGENTA,
    RED,
    YELLOW,
    PrefixedStream,
    colorize,
    mode_color,
    render_terminal_markdown,
)
from learncode_agent.tools import (
    edit_file,
    make_directory,
    make_tool_schema,
    read_file,
    run_bash_command,
    write_file,
)


HANDOFF_MARKER = "LEARNCODE_HANDOFF:"
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "prompts"
AUTO_PROMPTS = {"plan": "Build Plan", "build": "Start Building"}


@dataclass(frozen=True)
class ModeSpec:
    prereq_field: str | None
    prereq_error: str
    context_fields: tuple[str, ...]
    tools: dict[str, Callable]


MODES: dict[str, ModeSpec] = {
    "brainstorm": ModeSpec(
        prereq_field=None,
        prereq_error="",
        context_fields=(),
        tools={},
    ),
    "plan": ModeSpec(
        prereq_field="project_brief",
        prereq_error="Plan Mode needs a confirmed project brief first.",
        context_fields=("project_brief",),
        tools={},
    ),
    "build": ModeSpec(
        prereq_field="approved_plan",
        prereq_error="Build Mode needs an approved plan first.",
        context_fields=("approved_plan", "todo_functions"),
        tools={
            "read_file": read_file,
            "write_file": write_file,
            "edit_file": edit_file,
            "make_directory": make_directory,
            "run_bash_command": run_bash_command,
        },
    ),
    "critic": ModeSpec(
        prereq_field="todo_functions",
        prereq_error="Critic Mode needs the planned TODO function list first.",
        context_fields=("todo_functions",),
        tools={
            "read_file": read_file,
            "run_bash_command": run_bash_command,
        },
    ),
}
MODE_ORDER = list(MODES)
LEARNCODE_BANNER = r"""
 _      _____    _    ____  _   _  ____ ___  ____  _____
| |    | ____|  / \  |  _ \| \ | |/ ___/ _ \|  _ \| ____|
| |    |  _|   / _ \ | |_) |  \| | |  | | | | | | |  _|
| |___ | |___ / ___ \|  _ <| |\  | |__| |_| | |_| | |___
|_____||_____/_/   \_\_| \_\_| \_|\____\___/|____/|_____|
""".strip("\n")


@dataclass
class SessionState:
    mode: str = "brainstorm"
    project_brief: dict[str, Any] | None = None
    approved_plan: dict[str, Any] | None = None
    todo_functions: list[Any] = field(default_factory=list)
    messages_by_mode: dict[str, list[Any]] = field(
        default_factory=lambda: {mode: [] for mode in MODE_ORDER}
    )


class AgentOutputSink(Protocol):
    def assistant_delta(self, text: str) -> None:
        ...

    def status(self, message: str, mode: str | None = None) -> None:
        ...

    def error(self, message: str) -> None:
        ...

    def tool_call(self, function_name: str, kwargs: dict[str, Any]) -> None:
        ...


def load_system_prompt(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(f"System prompt file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        return f.read()


def load_environment(cwd: Path | None = None) -> None:
    working_dir = (cwd or Path.cwd()).resolve()
    load_dotenv(working_dir / ".env", override=False)

    project_env = PROJECT_ROOT / ".env"
    if project_env.parent != working_dir:
        load_dotenv(project_env, override=False)


def prompt_path_for_mode(mode: str) -> Path:
    return PROMPTS_DIR / f"{mode}_system_prompt.md"


def context_for_mode(mode: str, state: SessionState) -> dict[str, Any]:
    spec = MODES.get(mode)
    if spec is None:
        return {}
    return {field_name: getattr(state, field_name) for field_name in spec.context_fields}


def initial_messages_for_mode(mode: str, state: SessionState) -> list[dict[str, str]]:
    system_prompt = load_system_prompt(prompt_path_for_mode(mode))
    context = context_for_mode(mode, state)
    if context:
        system_prompt += "\n\nContext from previous mode:\n"
        system_prompt += json.dumps(context, indent=2)
    return [{"role": "system", "content": system_prompt}]


def create_session_state() -> SessionState:
    state = SessionState()
    state.messages_by_mode["brainstorm"] = initial_messages_for_mode("brainstorm", state)
    return state


def missing_requirement_message(mode: str) -> str:
    spec = MODES.get(mode)
    return spec.prereq_error if spec else ""


def can_enter_mode(mode: str, state: SessionState) -> bool:
    spec = MODES.get(mode)
    if spec is None:
        return False
    if spec.prereq_field is None:
        return True
    return bool(getattr(state, spec.prereq_field))


def enter_mode(state: SessionState, mode: str, reset_messages: bool = False) -> str:
    if mode not in MODE_ORDER:
        return f"Unknown mode: {mode}"
    if not can_enter_mode(mode, state):
        return missing_requirement_message(mode)

    state.mode = mode
    if reset_messages or not state.messages_by_mode[mode]:
        state.messages_by_mode[mode] = initial_messages_for_mode(mode, state)
    return f"Switched to {mode.capitalize()} Mode."


def cycle_mode(state: SessionState) -> str:
    current_index = MODE_ORDER.index(state.mode)
    next_mode = MODE_ORDER[(current_index + 1) % len(MODE_ORDER)]
    return enter_mode(state, next_mode)


def parse_mode_command(user_text: str) -> str | None:
    text = user_text.strip().lower()
    if text.startswith("/mode "):
        return text.removeprefix("/mode ").strip()
    if text.startswith("/") and text[1:] in MODES:
        return text[1:]
    return None


def extract_handoff(content: str) -> tuple[str, dict[str, Any] | None, str | None]:
    marker_index = content.find(HANDOFF_MARKER)
    if marker_index == -1:
        return content, None, None

    before_marker = content[:marker_index]
    after_marker = content[marker_index + len(HANDOFF_MARKER):]
    stripped_after_marker = after_marker.lstrip()
    try:
        payload, json_end = json.JSONDecoder().raw_decode(stripped_after_marker)
    except json.JSONDecodeError as exc:
        snippet = stripped_after_marker[:200]
        return content, None, (
            f"Invalid handoff JSON ({exc.msg} at line {exc.lineno} col {exc.colno}): {snippet!r}"
        )

    if not isinstance(payload, dict):
        return content, None, "Invalid handoff payload: expected a JSON object."

    visible_content = (before_marker + stripped_after_marker[json_end:]).strip()
    return visible_content, payload, None


def apply_handoff(state: SessionState, payload: dict[str, Any]) -> str:
    next_mode = payload.get("next_mode")
    if next_mode not in MODES:
        return "Invalid handoff payload: next_mode is missing or unknown."

    for field_name in MODES[next_mode].context_fields:
        if field_name in payload:
            setattr(state, field_name, payload[field_name])

    return enter_mode(state, next_mode, reset_messages=True)


def tool_functions_for_mode(mode: str) -> dict[str, Callable]:
    spec = MODES.get(mode)
    return dict(spec.tools) if spec else {}


def tool_schemas_for_functions(tool_functions: dict[str, Callable]) -> list[dict[str, Any]]:
    return [make_tool_schema(func) for func in tool_functions.values()]


class HandoffOutputFilter:
    def __init__(self) -> None:
        self.pending = ""
        self.hiding = False

    def feed(self, text: str) -> str:
        if self.hiding:
            return ""

        self.pending += text
        marker_index = self.pending.find(HANDOFF_MARKER)
        if marker_index != -1:
            output = self.pending[:marker_index]
            self.pending = ""
            self.hiding = True
            return output

        keep_chars = len(HANDOFF_MARKER) - 1
        if len(self.pending) <= keep_chars:
            return ""

        output = self.pending[:-keep_chars]
        self.pending = self.pending[-keep_chars:]
        return output

    def flush(self) -> str:
        if self.hiding:
            self.pending = ""
            return ""

        output = self.pending
        self.pending = ""
        return output


def prompt_text(state: SessionState) -> str:
    return f"[{state.mode.capitalize()}] > "


def styled_prompt_text(state: SessionState) -> str:
    label = colorize(f"[{state.mode.capitalize()}]", BOLD, mode_color(state.mode))
    return f"{colorize(' ', BG_DARK)}{label}{colorize(' > ', DIM, BG_DARK)}"


def print_status(message: str, mode: str | None = None) -> None:
    color = mode_color(mode) if mode else CYAN
    print(f"{colorize('●', BOLD, color)} {message}\n")


def print_error(message: str) -> None:
    print(f"{colorize(message, BOLD, RED)}\n")


def print_banner() -> None:
    print(colorize(LEARNCODE_BANNER, BOLD, CYAN))
    print()


def prompt_session_kwargs(state: SessionState) -> dict[str, Any]:
    return {"message": lambda: ANSI(styled_prompt_text(state))}


def append_user_message(state: SessionState, content: str) -> None:
    state.messages_by_mode[state.mode].append({"role": "user", "content": content})


def handle_streamed_handoff(
    state: SessionState,
    content: str,
    output_sink: AgentOutputSink | None = None,
) -> str | None:
    _, handoff_payload, handoff_error = extract_handoff(content)
    if handoff_error:
        if output_sink:
            output_sink.error(handoff_error)
        else:
            print_error(handoff_error)
    if handoff_payload:
        previous_mode = state.mode
        message = apply_handoff(state, handoff_payload)
        if output_sink:
            output_sink.status(message, state.mode)
        else:
            print_status(message, state.mode)
        if state.mode != previous_mode:
            return state.mode
    return None


def append_tool_call_delta(tool_calls: dict[int, dict[str, Any]], tool_call_delta: Any) -> None:
    index = getattr(tool_call_delta, "index", 0)
    tool_call = tool_calls.setdefault(
        index,
        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
    )

    tool_call_id = getattr(tool_call_delta, "id", None)
    if tool_call_id:
        tool_call["id"] = tool_call_id

    tool_call_type = getattr(tool_call_delta, "type", None)
    if tool_call_type:
        tool_call["type"] = tool_call_type

    function_delta = getattr(tool_call_delta, "function", None)
    if not function_delta:
        return

    function_name = getattr(function_delta, "name", None)
    if function_name:
        tool_call["function"]["name"] += function_name

    function_arguments = getattr(function_delta, "arguments", None)
    if function_arguments:
        tool_call["function"]["arguments"] += function_arguments


def stream_assistant_response(
    client: Any,
    request_kwargs: dict[str, Any],
    on_visible_text: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    stream = client.chat.completions.create(**request_kwargs, stream=True)
    content_parts: list[str] = []
    visible_parts: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    output_filter = HandoffOutputFilter()

    for chunk in stream:
        if not chunk.choices:
            continue

        delta = chunk.choices[0].delta
        content_delta = getattr(delta, "content", None)
        if content_delta:
            content_parts.append(content_delta)
            visible_delta = output_filter.feed(content_delta)
            if visible_delta:
                visible_parts.append(visible_delta)
                if on_visible_text:
                    on_visible_text(visible_delta)

        for tool_call_delta in getattr(delta, "tool_calls", None) or []:
            append_tool_call_delta(tool_calls, tool_call_delta)

    visible_tail = output_filter.flush()
    if visible_tail:
        visible_parts.append(visible_tail)
        if on_visible_text:
            on_visible_text(visible_tail)

    visible_content = "".join(visible_parts)
    if visible_content and not on_visible_text:
        assistant_output = PrefixedStream(colorize("●", BOLD) + " ")
        rendered_output = render_terminal_markdown(visible_content)
        terminal_output = assistant_output.feed(rendered_output)
        terminal_output += assistant_output.flush()
        print(f"\n{terminal_output}", end="", flush=True)
        print("\n")

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]
    return message


def run_agent_turn(
    client: Any,
    model: str,
    state: SessionState,
    output_sink: AgentOutputSink | None = None,
    tool_executor: Callable[[Callable, dict[str, Any]], Any] | None = None,
) -> str | None:
    messages = state.messages_by_mode[state.mode]

    while True:
        tool_functions = tool_functions_for_mode(state.mode)
        request_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
        }
        if tool_functions:
            request_kwargs["tools"] = tool_schemas_for_functions(tool_functions)
            request_kwargs["tool_choice"] = "auto"

        message = stream_assistant_response(
            client,
            request_kwargs,
            output_sink.assistant_delta if output_sink else None,
        )
        messages.append(message)

        if not message.get("tool_calls"):
            return handle_streamed_handoff(
                state,
                message.get("content") or "",
                output_sink,
            )

        for tool_call in message["tool_calls"]:
            function_name = tool_call["function"]["name"]
            func = tool_functions[function_name]
            kwargs = json.loads(tool_call["function"]["arguments"])
            if output_sink:
                output_sink.tool_call(function_name, kwargs)
            else:
                tool_label = f"{colorize('●', BOLD, YELLOW)} Tool call"
                function_label = colorize(function_name, BOLD, MAGENTA)
                print(f"{tool_label} {function_label}({json.dumps(kwargs, indent=2)})\n")
            result = tool_executor(func, kwargs) if tool_executor else func(**kwargs)
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call["id"],
                "content": str(result),
            })


def run_auto_prompt_for_mode(
    client: Any,
    model: str,
    state: SessionState,
    output_sink: AgentOutputSink | None = None,
    tool_executor: Callable[[Callable, dict[str, Any]], Any] | None = None,
) -> None:
    prompted_modes: set[str] = set()

    while state.mode not in prompted_modes:
        auto_prompt = AUTO_PROMPTS.get(state.mode)
        if not auto_prompt:
            return

        prompted_modes.add(state.mode)
        append_user_message(state, auto_prompt)
        if not run_agent_turn(client, model, state, output_sink, tool_executor):
            return


def main() -> int:
    from openai import OpenAI
    from learncode_agent.tui import run_tui

    load_environment()
    client = OpenAI()
    model = os.getenv("LEARNCODE_MODEL", "gpt-4.1-mini")
    state = create_session_state()
    return run_tui(client, model, state)


if __name__ == "__main__":
    raise SystemExit(main())
