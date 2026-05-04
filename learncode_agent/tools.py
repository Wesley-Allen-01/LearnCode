import difflib
import inspect
from pathlib import Path
import subprocess
import typing
from collections.abc import Callable

import docstring_parser

from learncode_agent.terminal_style import (
    BOLD,
    CYAN,
    GREEN,
    YELLOW,
    color_unified_diff,
    colorize,
)


_PYTHON_TO_JSON_TYPE = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def make_tool_schema(func: Callable) -> dict:
    """
    Build an OpenAI tool schema dict from a type-annotated, docstring-documented function.
    :param func: The function to convert into a tool schema.
    :return: A dict matching the OpenAI tools list entry format.
    """
    hints = typing.get_type_hints(func)
    sig = inspect.signature(func)
    doc = docstring_parser.parse(func.__doc__ or "")

    description = doc.short_description or ""
    if doc.long_description:
        description += "\n" + doc.long_description

    param_docs = {p.arg_name: p.description for p in doc.params}

    properties = {}
    required = []
    for name, param in sig.parameters.items():
        json_type = _PYTHON_TO_JSON_TYPE.get(hints.get(name), "string")
        prop: dict = {"type": json_type}
        if name in param_docs:
            prop["description"] = param_docs[name]
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def command_looks_like_file_mutation(command: str) -> bool:
    blocked_fragments = [
        ">",
        "tee ",
        "sed -i",
        "cat <<",
        "python - <<",
        "python3 - <<",
        "touch ",
        "mkdir ",
        "rm ",
        "rmdir ",
        "mv ",
        "cp ",
    ]
    one_line_command = command.replace("\n", " ")
    normalized = f" {one_line_command} "
    return any(fragment in normalized for fragment in blocked_fragments)


def run_bash_command(command: str) -> tuple[str, str, int]:
    """
    Run a read-only shell command or a test command.

    Do not use this tool to create, edit, move, copy, or delete files. Use
    make_directory, write_file, and edit_file for file changes.
    :param command: The read-only shell command or test command to run.
    :return: A tuple of (stdout, stderr, return_code).
    """
    if command_looks_like_file_mutation(command):
        return (
            "",
            "Command rejected: use make_directory, write_file, or edit_file for file changes.",
            1,
        )

    print(f"\n{colorize('Command to execute:', BOLD, YELLOW)} {command}")
    approval = input(colorize("Approve? [y/N]: ", BOLD, YELLOW)).strip().lower()
    if approval != "y":
        return "", "Command rejected by user.", 1
    result = subprocess.run(command, shell=True, capture_output=True, text=True)
    return result.stdout, result.stderr, result.returncode


def read_file(path: str) -> str:
    """
    Read the contents of a file.
    :param path: The path to the file to read.
    :return: The contents of the file as a string.
    """
    with open(path, "r") as f:
        return f.read()


def edit_file(path: str, old_string: str, new_string: str) -> str:
    """
    Edit an existing file by replacing one unique occurrence of old_string with new_string.

    Use this tool for every change to an existing file. Do not use shell
    commands, redirection, heredocs, Python scripts, sed, tee, or cat to edit
    files.
    :param path: The path to the file to edit.
    :param old_string: The exact string to find and replace in the file.
    :param new_string: The string to write in place of old_string.
    :return: A message indicating success, rejection, or the error that occurred.
    """
    with open(path, "r") as f:
        original = f.read()

    count = original.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}."
    if count > 1:
        return (
            f"Error: old_string appears {count} times in {path}. "
            "Provide more surrounding context to make it unique."
        )

    updated = original.replace(old_string, new_string, 1)

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    print(f"\n{colorize('Proposed edit to', BOLD, CYAN)} {path}:")
    print(color_unified_diff("".join(diff)))

    approval = input(colorize("Approve edit? [y/N]: ", BOLD, YELLOW)).strip().lower()
    if approval != "y":
        return "Edit rejected by user."

    with open(path, "w") as f:
        f.write(updated)

    return f"Successfully edited {path}."


def write_file(path: str, content: str) -> str:
    """
    Create a new file or replace the full contents of an existing file.

    Use this tool for file creation and full-file rewrites. Do not use shell
    commands, redirection, heredocs, Python scripts, tee, or cat to write files.
    :param path: The path of the file to create or overwrite.
    :param content: The complete contents to write to the file.
    :return: A message indicating success, rejection, or the error that occurred.
    """
    file_path = Path(path)
    if not file_path.parent.exists():
        return f"Error: parent directory does not exist: {file_path.parent}"

    original = ""
    if file_path.exists():
        original = file_path.read_text(encoding="utf-8")

    diff = difflib.unified_diff(
        original.splitlines(keepends=True),
        content.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    print(f"\n{colorize('Proposed write to', BOLD, CYAN)} {path}:")
    print(color_unified_diff("".join(diff)))

    approval = input(colorize("Approve write? [y/N]: ", BOLD, YELLOW)).strip().lower()
    if approval != "y":
        return "Write rejected by user."

    file_path.write_text(content, encoding="utf-8")
    return f"Successfully wrote {path}."


def make_directory(path: str) -> str:
    """
    Create a directory for project files.

    Use this tool instead of mkdir in the shell.
    :param path: The directory path to create.
    :return: A message indicating success, rejection, or the error that occurred.
    """
    print(f"\n{colorize('Directory to create:', BOLD, GREEN)} {path}")
    approval = (
        input(colorize("Approve directory creation? [y/N]: ", BOLD, YELLOW))
        .strip()
        .lower()
    )
    if approval != "y":
        return "Directory creation rejected by user."

    Path(path).mkdir(parents=True, exist_ok=True)
    return f"Successfully created directory {path}."
