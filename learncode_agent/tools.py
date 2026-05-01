import difflib
import inspect
import subprocess
import typing
from collections.abc import Callable

import docstring_parser


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


def run_bash_command(command: str) -> tuple[str, str, int]:
    """
    Execute a bash command string.
    :param command: The bash command to run (e.g. "mkdir new_directory").
    :return: A tuple of (stdout, stderr, return_code).
    """
    print(f"\nCommand to execute: {command}")
    approval = input("Approve? [y/N]: ").strip().lower()
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
    Edit a file by replacing one unique occurrence of old_string with new_string.
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
    print(f"\nProposed edit to {path}:")
    print("".join(diff))

    approval = input("Approve? [y/N]: ").strip().lower()
    if approval != "y":
        return "Edit rejected by user."

    with open(path, "w") as f:
        f.write(updated)

    return f"Successfully edited {path}."


def add_nums(a: int, b: int) -> int:
    """
    Add two numbers together.
    :param a: The first number to add.
    :param b: The second number to add.
    :return: The sum of a and b.
    
    """
    print("heyhey")
    return a + b


# print(add_nums.__doc__)


def subtract_nums(a: int, b: int) -> int:
    """
    Subtract one number from another.
    :param a: The number to subtract from.
    :param b: The number to subtract.
    :return: The difference of a and b.
    
    """
    return a - b

print(make_tool_schema(subtract_nums))