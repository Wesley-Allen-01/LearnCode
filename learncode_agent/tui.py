from __future__ import annotations

import builtins
import io
import json
import re
import shutil
import threading
import traceback
from collections.abc import Callable
from contextlib import contextmanager, redirect_stdout
from dataclasses import dataclass
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.formatted_text import StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style

from learncode_agent.main import (
    LEARNCODE_BANNER,
    SessionState,
    append_user_message,
    cycle_mode,
    enter_mode,
    parse_mode_command,
    run_agent_turn,
    run_auto_prompt_for_mode,
)


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


@dataclass
class TranscriptEvent:
    kind: str
    text: str
    mode: str | None = None


@dataclass
class ApprovalRequest:
    prompt: str
    submitted: threading.Event
    response: str = ""


class TerminalAgentTui:
    def __init__(self, client: Any, model: str, state: SessionState) -> None:
        self.client = client
        self.model = model
        self.state = state
        self.events: list[TranscriptEvent] = [TranscriptEvent("banner", LEARNCODE_BANNER)]
        self.input_buffer = Buffer(multiline=False, accept_handler=self.submit_input)
        self.lock = threading.RLock()
        self.is_busy = False
        self.current_assistant_event: TranscriptEvent | None = None
        self.pending_approval: ApprovalRequest | None = None
        self.pending_tool_summary = ""
        self.transcript_scroll_offset = 0
        self.app: Application | None = None

    def run(self) -> int:
        self.app = self.build_application()
        return self.app.run() or 0

    def build_application(
        self,
        app_input: Any | None = None,
        app_output: Any | None = None,
    ) -> Application:
        key_bindings = KeyBindings()

        @key_bindings.add("s-tab")
        def _(event: Any) -> None:
            self.cycle_mode()

        @key_bindings.add("pageup")
        def _(event: Any) -> None:
            self.scroll_transcript_page_up()

        @key_bindings.add("pagedown")
        def _(event: Any) -> None:
            self.scroll_transcript_page_down()

        @key_bindings.add("c-home")
        def _(event: Any) -> None:
            self.scroll_transcript_to_top()

        @key_bindings.add("c-end")
        def _(event: Any) -> None:
            self.scroll_transcript_to_bottom()

        @key_bindings.add("c-c")
        @key_bindings.add("c-d")
        def _(event: Any) -> None:
            event.app.exit(result=0)

        input_control = BufferControl(buffer=self.input_buffer)
        transcript = Window(
            content=FormattedTextControl(
                self.render_transcript,
                get_cursor_position=self.transcript_cursor_position,
                show_cursor=False,
            ),
            wrap_lines=False,
            get_vertical_scroll=self.transcript_vertical_scroll,
            always_hide_cursor=True,
            style="class:body",
        )
        input_row = VSplit(
            [
                Window(
                    content=FormattedTextControl([("class:prompt", "› ")]),
                    width=2,
                    style="class:input",
                ),
                Window(
                    content=input_control,
                    height=1,
                    style="class:input",
                ),
            ],
            height=1,
            style="class:input",
        )
        root = HSplit(
            [
                transcript,
                Window(char="─", height=1, style="class:separator"),
                input_row,
                Window(char="─", height=1, style="class:separator"),
                Window(
                    content=FormattedTextControl(self.render_status),
                    height=1,
                    style="class:status",
                ),
            ],
            style="class:body",
        )
        return Application(
            layout=Layout(root, focused_element=input_control),
            key_bindings=key_bindings,
            input=app_input,
            output=app_output,
            style=Style.from_dict({
                "body": "bg:#282c34 #f8f8f2",
                "input": "bg:#282c34 #f8f8f2",
                "prompt": "bg:#282c34 #ffffff bold",
                "separator": "bg:#282c34 #8a8f98",
                "status": "bg:#282c34 #9da0a6",
                "status-quote": "bg:#282c34 #4db6ac bold",
                "status-mode": "bg:#282c34 #4db6ac",
                "status-hint": "bg:#282c34 #9da0a6",
                "user-row": "bg:#3a3a38 #f8f8f2",
                "user-chevron": "bg:#3a3a38 #555852 bold",
                "assistant-dot": "bg:#282c34 #ffffff bold",
                "assistant": "bg:#282c34 #f8f8f2",
                "tool": "bg:#282c34 #d7ba7d",
                "diff-add": "bg:#183d24 #c6f6d5",
                "diff-del": "bg:#4a1f1f #fed7d7",
                "status-event": "bg:#282c34 #4db6ac",
                "error": "bg:#282c34 #f48771 bold",
                "banner": "bg:#282c34 #4db6ac bold",
            }),
            full_screen=True,
            mouse_support=True,
        )

    def submit_input(self, buffer: Buffer) -> bool:
        text = buffer.text.strip()
        buffer.reset()
        if self.pending_approval:
            self.submit_approval(text)
            return True
        if not text:
            return True
        if text in ("quit", "q"):
            if self.app:
                self.app.exit(result=0)
            return True
        if self.is_busy:
            self.status("Agent is still responding.", self.state.mode)
            return True

        self.scroll_transcript_to_bottom()
        requested_mode = parse_mode_command(text)
        if requested_mode:
            self.enter_requested_mode(requested_mode)
            return True

        self.add_event("user", text)
        append_user_message(self.state, text)
        self.start_agent_turn()
        return True

    def enter_requested_mode(self, requested_mode: str) -> None:
        previous_mode = self.state.mode
        message = enter_mode(self.state, requested_mode)
        self.status(message, self.state.mode)
        if self.state.mode != previous_mode:
            self.start_auto_prompts()

    def cycle_mode(self) -> None:
        if self.is_busy:
            self.status("Agent is still responding.", self.state.mode)
            return
        previous_mode = self.state.mode
        message = cycle_mode(self.state)
        self.status(message, self.state.mode)
        if self.state.mode != previous_mode:
            self.start_auto_prompts()

    def start_agent_turn(self) -> None:
        self.start_worker(self.run_agent_worker)

    def start_auto_prompts(self) -> None:
        self.start_worker(self.run_auto_prompt_worker)

    def start_worker(self, target: Callable[[], None]) -> None:
        with self.lock:
            if self.is_busy:
                self.status("Agent is still responding.", self.state.mode)
                return
            self.is_busy = True
            self.current_assistant_event = None
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        self.invalidate()

    def run_agent_worker(self) -> None:
        try:
            changed_mode = run_agent_turn(
                self.client,
                self.model,
                self.state,
                output_sink=self,
                tool_executor=self.execute_tool,
            )
            if changed_mode:
                run_auto_prompt_for_mode(
                    self.client,
                    self.model,
                    self.state,
                    output_sink=self,
                    tool_executor=self.execute_tool,
                )
        except Exception:
            self.error(traceback.format_exc().strip())
        finally:
            self.finish_worker()

    def run_auto_prompt_worker(self) -> None:
        try:
            run_auto_prompt_for_mode(
                self.client,
                self.model,
                self.state,
                output_sink=self,
                tool_executor=self.execute_tool,
            )
        except Exception:
            self.error(traceback.format_exc().strip())
        finally:
            self.finish_worker()

    def finish_worker(self) -> None:
        with self.lock:
            self.is_busy = False
            self.current_assistant_event = None
        self.invalidate()

    def assistant_delta(self, text: str) -> None:
        with self.lock:
            if not self.current_assistant_event:
                self.current_assistant_event = TranscriptEvent(
                    "assistant",
                    "",
                    self.state.mode,
                )
                self.events.append(self.current_assistant_event)
            self.current_assistant_event.text += text
        self.invalidate()

    def status(self, message: str, mode: str | None = None) -> None:
        self.add_event("status", message, mode)

    def error(self, message: str) -> None:
        self.add_event("error", message)

    def tool_call(self, function_name: str, kwargs: dict[str, Any]) -> None:
        self.current_assistant_event = None
        self.pending_tool_summary = format_tool_call_summary(function_name, kwargs)
        self.add_event("tool", self.pending_tool_summary)

    def tool_output(self, text: str) -> None:
        self.current_assistant_event = None
        stripped_text = strip_ansi(text).strip()
        if stripped_text:
            self.add_event("tool", stripped_text)

    def add_event(self, kind: str, text: str, mode: str | None = None) -> None:
        with self.lock:
            self.current_assistant_event = None if kind != "assistant" else self.current_assistant_event
            self.events.append(TranscriptEvent(kind, text, mode))
        self.invalidate()

    def execute_tool(self, func: Callable, kwargs: dict[str, Any]) -> Any:
        output = io.StringIO()
        try:
            with redirect_stdout(output), self.patch_input(output):
                return func(**kwargs)
        finally:
            self.tool_output(output.getvalue())
            self.pending_tool_summary = ""

    @contextmanager
    def patch_input(self, output: io.StringIO) -> Any:
        original_input = builtins.input

        def tui_input(prompt: str = "") -> str:
            self.flush_tool_output(output)
            return self.request_approval(prompt)

        builtins.input = tui_input
        try:
            yield
        finally:
            builtins.input = original_input

    def flush_tool_output(self, output: io.StringIO) -> None:
        text = output.getvalue()
        output.seek(0)
        output.truncate(0)
        self.tool_output(text)

    def request_approval(self, prompt: str) -> str:
        request = ApprovalRequest(strip_ansi(prompt).strip(), threading.Event())
        with self.lock:
            self.pending_approval = request
        message = f"{request.prompt} Type y and press Enter to approve."
        if self.pending_tool_summary:
            message = f"Pending {self.pending_tool_summary}\n{message}"
        self.add_event("tool", message)
        request.submitted.wait()
        with self.lock:
            if self.pending_approval is request:
                self.pending_approval = None
        return request.response

    def submit_approval(self, text: str) -> None:
        with self.lock:
            request = self.pending_approval
            if not request:
                return
            request.response = text
            request.submitted.set()
            self.pending_approval = None
        self.add_event("tool", f"Approval response: {text or 'n'}")

    def render_transcript(self) -> StyleAndTextTuples:
        width = shutil.get_terminal_size((80, 20)).columns
        lines = self.visible_transcript_lines(width)

        fragments: StyleAndTextTuples = []
        for line in lines:
            fragments.extend(self.with_transcript_mouse_handler(line, width))
            fragments.append(("", "\n"))
        return fragments

    def transcript_cursor_position(self) -> Point:
        width = shutil.get_terminal_size((80, 20)).columns
        line_count = len(self.visible_transcript_lines(width))
        return Point(x=0, y=max(0, line_count - 1))

    def transcript_vertical_scroll(self, window: Window) -> int:
        return 0

    def transcript_top_line(self, line_count: int) -> int:
        height = self.transcript_height()
        self.transcript_scroll_offset = min(
            self.transcript_scroll_offset,
            self.max_transcript_scroll_offset(line_count),
        )
        return max(0, line_count - height - self.transcript_scroll_offset)

    def max_transcript_scroll_offset(self, line_count: int | None = None) -> int:
        if line_count is None:
            width = shutil.get_terminal_size((80, 20)).columns
            line_count = len(self.rendered_transcript_lines(width))
        return max(0, line_count - self.transcript_height())

    def transcript_height(self) -> int:
        return max(1, shutil.get_terminal_size((80, 20)).lines - 4)

    def transcript_page_size(self) -> int:
        return max(1, self.transcript_height() - 1)

    def scroll_transcript_page_up(self) -> None:
        self.scroll_transcript(self.transcript_page_size())

    def scroll_transcript_page_down(self) -> None:
        self.scroll_transcript(-self.transcript_page_size())

    def scroll_transcript(self, delta: int) -> None:
        self.transcript_scroll_offset = max(
            0,
            min(
                self.transcript_scroll_offset + delta,
                self.max_transcript_scroll_offset(),
            ),
        )
        self.invalidate()

    def scroll_transcript_to_bottom(self) -> None:
        self.transcript_scroll_offset = 0
        self.invalidate()

    def scroll_transcript_to_top(self) -> None:
        self.transcript_scroll_offset = self.max_transcript_scroll_offset()
        self.invalidate()

    def visible_transcript_lines(self, width: int) -> list[StyleAndTextTuples]:
        lines = self.rendered_transcript_lines(width)
        top_line = self.transcript_top_line(len(lines))
        return lines[top_line:top_line + self.transcript_height()]

    def rendered_transcript_lines(self, width: int) -> list[StyleAndTextTuples]:
        lines: list[StyleAndTextTuples] = []
        with self.lock:
            events = list(self.events)

        for event in events:
            lines.extend(render_event_lines(event, width))
        return lines

    def with_transcript_mouse_handler(
        self,
        line: StyleAndTextTuples,
        width: int,
    ) -> StyleAndTextTuples:
        fragments: StyleAndTextTuples = []
        text_width = 0
        for fragment in line:
            text_width += len(fragment[1])
            if len(fragment) >= 3:
                fragments.append(fragment)
            else:
                fragments.append((fragment[0], fragment[1], self.handle_transcript_mouse))

        if text_width < width:
            fragments.append(
                ("class:body", " " * (width - text_width), self.handle_transcript_mouse)
            )
        return fragments

    def handle_transcript_mouse(self, mouse_event: MouseEvent) -> None:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.scroll_transcript(3)
        elif mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.scroll_transcript(-3)

    def render_status(self) -> StyleAndTextTuples:
        busy_text = " working" if self.is_busy else ""
        approval_text = " approve tool request" if self.pending_approval else ""
        scroll_text = (
            f" scrolled {self.transcript_scroll_offset} lines"
            if self.transcript_scroll_offset
            else ""
        )
        return [
            ("class:status-quote", ' "'),
            ("class:status-mode", f" {self.state.mode} mode on"),
            (
                "class:status-hint",
                f"{busy_text}{approval_text}{scroll_text} (pgup/pgdn or wheel scroll, shift+tab mode)",
            ),
        ]

    def invalidate(self) -> None:
        if self.app:
            self.app.invalidate()


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def format_tool_call_summary(function_name: str, kwargs: dict[str, Any]) -> str:
    arguments = {
        key: summarize_tool_argument(value)
        for key, value in kwargs.items()
    }
    return f"Tool call {function_name}({json.dumps(arguments, indent=2)})"


def summarize_tool_argument(value: Any) -> Any:
    if not isinstance(value, str):
        return value

    single_line = value.replace("\n", "\\n")
    if len(single_line) <= 240:
        return single_line
    return f"{single_line[:240]}... [{len(value)} chars total]"


def render_event_lines(event: TranscriptEvent, width: int) -> list[StyleAndTextTuples]:
    text = event.text.rstrip("\n")
    if event.kind == "user":
        content = f"› {text}"
        padded = content[:width].ljust(width)
        return [[("class:user-chevron", padded[:2]), ("class:user-row", padded[2:])]]

    if event.kind == "assistant":
        return prefixed_lines(text, "● ", "class:assistant-dot", "class:assistant")

    if event.kind == "banner":
        return [[("class:banner", line)] for line in text.splitlines()]

    if event.kind == "tool":
        return tool_event_lines(text)

    if event.kind == "error":
        return prefixed_lines(text, "● ", "class:error", "class:error")

    return prefixed_lines(text, "● ", "class:status-event", "class:status-event")


def prefixed_lines(
    text: str,
    prefix: str,
    prefix_style: str,
    text_style: str,
) -> list[StyleAndTextTuples]:
    raw_lines = text.splitlines() or [""]
    lines: list[StyleAndTextTuples] = []
    for index, line in enumerate(raw_lines):
        if index == 0:
            lines.append([(prefix_style, prefix), (text_style, line)])
        else:
            lines.append([(text_style, line)])
    return lines


def tool_event_lines(text: str) -> list[StyleAndTextTuples]:
    raw_lines = text.splitlines() or [""]
    lines: list[StyleAndTextTuples] = []
    for index, line in enumerate(raw_lines):
        text_style = diff_line_style(line)
        if index == 0:
            lines.append([("class:tool", "● "), (text_style, line)])
        else:
            lines.append([(text_style, line)])
    return lines


def diff_line_style(line: str) -> str:
    if line.startswith("+") and not line.startswith("+++"):
        return "class:diff-add"
    if line.startswith("-") and not line.startswith("---"):
        return "class:diff-del"
    return "class:tool"


def run_tui(client: Any, model: str, state: SessionState) -> int:
    return TerminalAgentTui(client, model, state).run()
