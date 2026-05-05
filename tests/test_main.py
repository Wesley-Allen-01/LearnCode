import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput

from learncode_agent.main import (
    AUTO_PROMPTS,
    HandoffOutputFilter,
    LEARNCODE_BANNER,
    SessionState,
    append_tool_call_delta,
    apply_handoff,
    can_enter_mode,
    create_session_state,
    extract_handoff,
    load_environment,
    load_system_prompt,
    print_banner,
    prompt_text,
    prompt_session_kwargs,
    run_auto_prompt_for_mode,
    stream_assistant_response,
    tool_functions_for_mode,
)
from learncode_agent.terminal_style import PrefixedStream, colorize, render_terminal_markdown
from learncode_agent.tui import TerminalAgentTui, TranscriptEvent, render_event_lines, strip_ansi
from learncode_agent.tools import command_looks_like_file_mutation


class PromptLoadingTests(unittest.TestCase):
    def test_load_system_prompt_reads_file_contents(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt_path = Path(temp_dir) / "prompt.md"
            prompt_path.write_text("Use Brainstorm Mode.", encoding="utf-8")

            self.assertEqual(load_system_prompt(prompt_path), "Use Brainstorm Mode.")

    def test_load_system_prompt_missing_file_raises_clear_error(self):
        missing_path = Path("does_not_exist.md")

        with self.assertRaisesRegex(FileNotFoundError, "System prompt file not found"):
            load_system_prompt(missing_path)


class EnvironmentLoadingTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("LEARNCODE_TEST_FROM_DOTENV", None)
        os.environ.pop("LEARNCODE_TEST_EXISTING_ENV", None)

    def test_load_environment_reads_cwd_dotenv(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("LEARNCODE_TEST_FROM_DOTENV=loaded\n", encoding="utf-8")

            load_environment(Path(temp_dir))

            self.assertEqual(os.environ["LEARNCODE_TEST_FROM_DOTENV"], "loaded")

    def test_load_environment_does_not_override_existing_env(self):
        os.environ["LEARNCODE_TEST_EXISTING_ENV"] = "from-shell"
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("LEARNCODE_TEST_EXISTING_ENV=from-file\n", encoding="utf-8")

            load_environment(Path(temp_dir))

            self.assertEqual(os.environ["LEARNCODE_TEST_EXISTING_ENV"], "from-shell")


class ModePrerequisiteTests(unittest.TestCase):
    def test_plan_requires_project_brief(self):
        state = SessionState()

        self.assertFalse(can_enter_mode("plan", state))

        state.project_brief = {"project_name": "Example"}
        self.assertTrue(can_enter_mode("plan", state))

    def test_build_requires_approved_plan(self):
        state = SessionState()

        self.assertFalse(can_enter_mode("build", state))

        state.approved_plan = {"steps": []}
        self.assertTrue(can_enter_mode("build", state))

    def test_critic_requires_todo_functions(self):
        state = SessionState()

        self.assertFalse(can_enter_mode("critic", state))

        state.todo_functions = [{"name": "score_item"}]
        self.assertTrue(can_enter_mode("critic", state))


class ToolPolicyTests(unittest.TestCase):
    def test_build_mode_has_file_tools(self):
        self.assertEqual(
            sorted(tool_functions_for_mode("build")),
            ["edit_file", "make_directory", "read_file", "run_bash_command", "write_file"],
        )

    def test_shell_file_mutation_commands_are_blocked(self):
        blocked_commands = [
            "cat <<'EOF' > app.py",
            "python3 - <<'PY'",
            "sed -i '' 's/a/b/' app.py",
            "mkdir project",
            "touch app.py",
        ]

        for command in blocked_commands:
            self.assertTrue(command_looks_like_file_mutation(command))

    def test_read_only_shell_commands_are_allowed(self):
        allowed_commands = [
            "ls",
            "pwd",
            "python -m unittest discover -s tests",
            "git diff --check",
        ]

        for command in allowed_commands:
            self.assertFalse(command_looks_like_file_mutation(command))


class HandoffParsingTests(unittest.TestCase):
    def test_valid_handoff_updates_state_and_switches_mode(self):
        state = create_session_state()
        content = (
            "Project brief confirmed.\n"
            'LEARNCODE_HANDOFF: {"next_mode":"plan","project_brief":{"project_name":"Quiz CLI"}}'
        )

        visible_content, payload, error = extract_handoff(content)
        self.assertEqual(visible_content, "Project brief confirmed.")
        self.assertIsNone(error)

        message = apply_handoff(state, payload)

        self.assertEqual(message, "Switched to Plan Mode.")
        self.assertEqual(state.mode, "plan")
        self.assertEqual(state.project_brief, {"project_name": "Quiz CLI"})

    def test_invalid_handoff_json_returns_error(self):
        content = 'LEARNCODE_HANDOFF: {"next_mode":"plan"'

        visible_content, payload, error = extract_handoff(content)

        self.assertEqual(visible_content, content)
        self.assertIsNone(payload)
        self.assertIn("Invalid handoff JSON", error)


class StreamingHelperTests(unittest.TestCase):
    def test_handoff_output_filter_hides_marker_and_payload(self):
        output_filter = HandoffOutputFilter()
        chunks = [
            "Project confirmed.\nLEAR",
            "NCODE_HANDOFF:",
            ' {"next_mode":"plan"}',
        ]

        visible_output = "".join(output_filter.feed(chunk) for chunk in chunks)
        visible_output += output_filter.flush()

        self.assertEqual(visible_output, "Project confirmed.\n")

    def test_handoff_output_filter_flushes_normal_text(self):
        output_filter = HandoffOutputFilter()

        visible_output = output_filter.feed("Hello")
        visible_output += output_filter.feed(", world.")
        visible_output += output_filter.flush()

        self.assertEqual(visible_output, "Hello, world.")

    def test_append_tool_call_delta_accumulates_streamed_arguments(self):
        tool_calls = {}
        first_delta = SimpleNamespace(
            index=0,
            id="call_123",
            type="function",
            function=SimpleNamespace(name="read_file", arguments='{"path"'),
        )
        second_delta = SimpleNamespace(
            index=0,
            id=None,
            type=None,
            function=SimpleNamespace(name=None, arguments=':"main.py"}'),
        )

        append_tool_call_delta(tool_calls, first_delta)
        append_tool_call_delta(tool_calls, second_delta)

        self.assertEqual(
            tool_calls[0],
            {
                "id": "call_123",
                "type": "function",
                "function": {"name": "read_file", "arguments": '{"path":"main.py"}'},
            },
        )

    def test_stream_assistant_response_renders_visible_output_as_markdown(self):
        class FakeCompletions:
            def create(self, **kwargs):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="**Hello**\nWor")
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="ld")
                        )
                    ]
                )

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )
        output = io.StringIO()

        with redirect_stdout(output):
            message = stream_assistant_response(
                client, {"model": "test", "messages": []}
            )

        self.assertEqual(message["content"], "**Hello**\nWorld")
        self.assertIn("● Hello World", output.getvalue())
        self.assertTrue(output.getvalue().startswith("\n● Hello World"))
        self.assertNotIn("**Hello**", output.getvalue())

    def test_stream_assistant_response_can_stream_to_callback_without_stdout(self):
        class FakeCompletions:
            def create(self, **kwargs):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="Hello\nLEAR")
                        )
                    ]
                )
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content='NCODE_HANDOFF: {"next_mode":"plan"}')
                        )
                    ]
                )

        client = SimpleNamespace(
            chat=SimpleNamespace(completions=FakeCompletions())
        )
        visible_chunks = []
        output = io.StringIO()

        with redirect_stdout(output):
            message = stream_assistant_response(
                client,
                {"model": "test", "messages": []},
                on_visible_text=visible_chunks.append,
            )

        self.assertEqual(message["content"], 'Hello\nLEARNCODE_HANDOFF: {"next_mode":"plan"}')
        self.assertEqual("".join(visible_chunks), "Hello\n")
        self.assertEqual(output.getvalue(), "")

    def test_auto_prompts_cover_plan_and_build_only(self):
        self.assertEqual(AUTO_PROMPTS, {"plan": "Build Plan", "build": "Start Building"})
        self.assertIsNone(AUTO_PROMPTS.get("critic"))

    def test_run_auto_prompt_for_plan_mode_sends_build_plan(self):
        class FakeCompletions:
            def create(self, **kwargs):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="Planning now.")
                        )
                    ]
                )

        state = SessionState(mode="plan", project_brief={"project_name": "Quiz CLI"})
        state.messages_by_mode["plan"] = [{"role": "system", "content": "Plan mode."}]
        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        with redirect_stdout(io.StringIO()):
            run_auto_prompt_for_mode(client, "test", state)

        self.assertEqual(
            state.messages_by_mode["plan"][1],
            {"role": "user", "content": "Build Plan"},
        )
        self.assertEqual(state.messages_by_mode["plan"][2]["content"], "Planning now.")

    def test_run_auto_prompt_for_build_mode_sends_start_building(self):
        class FakeCompletions:
            def create(self, **kwargs):
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            delta=SimpleNamespace(content="Building now.")
                        )
                    ]
                )

        state = SessionState(mode="build", approved_plan={"steps": []})
        state.messages_by_mode["build"] = [{"role": "system", "content": "Build mode."}]
        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        with redirect_stdout(io.StringIO()):
            run_auto_prompt_for_mode(client, "test", state)

        self.assertEqual(
            state.messages_by_mode["build"][1],
            {"role": "user", "content": "Start Building"},
        )
        self.assertEqual(state.messages_by_mode["build"][2]["content"], "Building now.")

    def test_auto_prompt_continues_after_plan_handoff_to_build(self):
        class FakeCompletions:
            def __init__(self):
                self.calls = 0

            def create(self, **kwargs):
                self.calls += 1
                if self.calls == 1:
                    content = (
                        "Plan approved.\n"
                        "LEARNCODE_HANDOFF: "
                        '{"next_mode":"build","approved_plan":{"steps":[]}}'
                    )
                else:
                    content = "Building now."
                yield SimpleNamespace(
                    choices=[
                        SimpleNamespace(delta=SimpleNamespace(content=content))
                    ]
                )

        state = SessionState(mode="plan", project_brief={"project_name": "Quiz CLI"})
        state.messages_by_mode["plan"] = [{"role": "system", "content": "Plan mode."}]
        state.messages_by_mode["build"] = [{"role": "system", "content": "Build mode."}]
        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        with redirect_stdout(io.StringIO()):
            run_auto_prompt_for_mode(client, "test", state)

        self.assertEqual(state.mode, "build")
        self.assertEqual(
            state.messages_by_mode["plan"][1],
            {"role": "user", "content": "Build Plan"},
        )
        self.assertEqual(
            state.messages_by_mode["build"][1],
            {"role": "user", "content": "Start Building"},
        )


class TerminalStyleTests(unittest.TestCase):
    def test_prefixed_stream_prefixes_only_first_visible_line(self):
        stream = PrefixedStream()

        first_output = stream.feed("Hello\nWor")
        final_output = stream.flush()

        self.assertEqual(first_output, "● Hello\n")
        self.assertEqual(final_output, "Wor")

    def test_colorize_can_be_disabled_for_plain_output(self):
        self.assertEqual(colorize("hello", "\033[32m", enable=False), "hello")

    def test_render_terminal_markdown_converts_basic_markdown(self):
        rendered_output = render_terminal_markdown("**Important**")

        self.assertIn("Important", rendered_output)
        self.assertNotIn("**Important**", rendered_output)

    def test_prompt_session_kwargs_uses_supported_prompt_arguments(self):
        kwargs = prompt_session_kwargs(SessionState())

        self.assertEqual(sorted(kwargs), ["message"])

    def test_prompt_text_includes_current_mode(self):
        self.assertEqual(prompt_text(SessionState(mode="build")), "[Build] > ")

    def test_print_banner_writes_learncode_ascii_art(self):
        output = io.StringIO()

        with redirect_stdout(output):
            print_banner()

        self.assertIn(" _      _____", output.getvalue())
        self.assertIn("|_____||_____", output.getvalue())
        self.assertEqual(len(LEARNCODE_BANNER.splitlines()), 5)


class TuiRenderingTests(unittest.TestCase):
    def test_strip_ansi_removes_color_codes(self):
        self.assertEqual(strip_ansi("\033[1mApprove?\033[0m"), "Approve?")

    def test_render_user_event_uses_highlighted_prompt_row(self):
        lines = render_event_lines(TranscriptEvent("user", "hi"), width=10)

        self.assertEqual(lines[0][0], ("class:user-chevron", "› "))
        self.assertEqual(lines[0][1], ("class:user-row", "hi      "))

    def test_render_assistant_event_uses_dot_prefix(self):
        lines = render_event_lines(TranscriptEvent("assistant", "Hi!"), width=80)

        self.assertEqual(lines[0], [("class:assistant-dot", "● "), ("class:assistant", "Hi!")])

    def test_status_line_shows_mode_and_shortcut_hint(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState(mode="plan"))

        rendered_status = "".join(text for _, text in ui.render_status())

        self.assertIn("plan mode on", rendered_status)
        self.assertIn("shift+tab to cycle", rendered_status)

    def test_transcript_renderer_keeps_full_history_for_scrollback(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events = [
            TranscriptEvent("assistant", "First response"),
            TranscriptEvent("user", "Second message"),
        ]

        rendered_transcript = "".join(text for _, text in ui.render_transcript())

        self.assertIn("First response", rendered_transcript)
        self.assertIn("Second message", rendered_transcript)

    def test_application_uses_normal_terminal_buffer_for_scrollback(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())

        app = ui.build_application(DummyInput(), DummyOutput())

        self.assertFalse(app.full_screen)

    def test_tool_preview_flushes_before_approval_request(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        saw_preview_before_approval = False

        def fake_tool():
            print("Preview")
            return input("Approve? [y/N]: ")

        def fake_request_approval(prompt):
            nonlocal saw_preview_before_approval
            saw_preview_before_approval = bool(ui.events and ui.events[0].text == "Preview")
            return "y"

        ui.request_approval = fake_request_approval

        result = ui.execute_tool(fake_tool, {})

        self.assertEqual(result, "y")
        self.assertTrue(saw_preview_before_approval)


if __name__ == "__main__":
    unittest.main()
