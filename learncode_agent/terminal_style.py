from __future__ import annotations

import io
import os
import shutil
import sys
from typing import TextIO


RESET = "\033[0m"
BOLD = "\033[1m"
DIM = "\033[2m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
CYAN = "\033[36m"
BG_DARK = "\033[48;5;236m"
BG_RED = "\033[48;5;52m"
BG_GREEN = "\033[48;5;22m"


MODE_COLORS = {
    "brainstorm": MAGENTA,
    "plan": BLUE,
    "build": GREEN,
    "critic": YELLOW,
}


def should_color(stream: TextIO | None = None) -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    return (stream or sys.stdout).isatty()


def colorize(text: str, *codes: str, enable: bool | None = None) -> str:
    if enable is None:
        enable = should_color()
    if not enable or not codes:
        return text
    return f"{''.join(codes)}{text}{RESET}"


def render_terminal_markdown(text: str) -> str:
    from rich.console import Console
    from rich.markdown import Markdown

    if not text:
        return ""

    color_enabled = should_color()
    buffer = io.StringIO()
    console = Console(
        file=buffer,
        force_terminal=color_enabled,
        color_system="auto" if color_enabled else None,
        highlight=False,
        soft_wrap=True,
        width=shutil.get_terminal_size((80, 20)).columns,
    )
    console.print(Markdown(text), end="")
    return buffer.getvalue()


def mode_color(mode: str) -> str:
    return MODE_COLORS.get(mode, CYAN)


class PrefixedStream:
    def __init__(self, prefix: str = "● ") -> None:
        self.prefix = prefix
        self.pending = ""
        self.started = False

    def feed(self, text: str) -> str:
        self.pending += text
        output_parts: list[str] = []

        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            output_parts.append(self._prefix_once(line) + "\n")

        return "".join(output_parts)

    def flush(self) -> str:
        if not self.pending:
            return ""

        output = self._prefix_once(self.pending)
        self.pending = ""
        return output

    def _prefix_once(self, text: str) -> str:
        if self.started:
            return text

        self.started = True
        return f"{self.prefix}{text}"


def color_unified_diff(diff_text: str, enable: bool | None = None) -> str:
    colored_lines = []
    for line in diff_text.splitlines(keepends=True):
        if line.startswith(("---", "+++", "@@")):
            colored_lines.append(colorize(line, CYAN, BOLD, enable=enable))
        elif line.startswith("+"):
            colored_lines.append(colorize(line, GREEN, BG_GREEN, enable=enable))
        elif line.startswith("-"):
            colored_lines.append(colorize(line, RED, BG_RED, enable=enable))
        else:
            colored_lines.append(line)
    return "".join(colored_lines)
