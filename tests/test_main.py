import os
import threading
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import DummyInput
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import DummyOutput

from learncode_agent.main import (
    AUTO_PROMPTS,
    HandoffOutputFilter,
    LEARNCODE_BANNER,
    SessionState,
    append_tool_call_delta,
    apply_handoff,
    create_session_state,
    enter_mode,
    extract_handoff,
    initial_messages_for_mode,
    load_environment,
    load_system_prompt,
    run_auto_prompt_for_mode,
    stream_assistant_response,
    tool_functions_for_mode,
)
from learncode_agent.terminal_style import (
    BG_GREEN,
    BG_RED,
    color_unified_diff,
    colorize,
    render_terminal_markdown,
)
from learncode_agent.tui import (
    TerminalAgentTui,
    TranscriptEvent,
    format_tool_call_summary,
    render_event_lines,
    strip_ansi,
    tool_event_lines,
)
from learncode_agent.tools import command_looks_like_file_mutation


def fragment_text(fragments):
    return "".join(fragment[1] for fragment in fragments)


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


class ModeSwitchingTests(unittest.TestCase):
    def test_enter_mode_switches_without_artifact(self):
        state = SessionState()

        message = enter_mode(state, "build")

        self.assertEqual(message, "Switched to Build Mode.")
        self.assertEqual(state.mode, "build")

    def test_unknown_mode_is_rejected(self):
        state = SessionState()

        message = enter_mode(state, "review")

        self.assertEqual(message, "Unknown mode: review")
        self.assertEqual(state.mode, "brainstorm")


class ModePromptContextTests(unittest.TestCase):
    def test_plan_prompt_notes_missing_project_brief(self):
        messages = initial_messages_for_mode("plan", SessionState())

        self.assertIn(
            "Missing expected context: project_brief. Ask the user for what you need before proceeding.",
            messages[0]["content"],
        )

    def test_build_prompt_treats_missing_approved_plan_as_optional(self):
        messages = initial_messages_for_mode("build", SessionState())

        self.assertIn(
            "Missing optional context: approved_plan. Do not require an approved plan;",
            messages[0]["content"],
        )
        self.assertIn("user's current request and repository context", messages[0]["content"])

    def test_truthy_artifacts_are_injected_as_context(self):
        state = SessionState(project_brief={"project_name": "Quiz CLI"})

        messages = initial_messages_for_mode("plan", state)

        self.assertIn("Context from previous mode:", messages[0]["content"])
        self.assertIn('"project_name": "Quiz CLI"', messages[0]["content"])
        self.assertNotIn("Missing expected context", messages[0]["content"])


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

    def test_stream_assistant_response_delivers_visible_text_to_callback(self):
        class FakeCompletions:
            def create(self, **kwargs):
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content="Hello\nLEAR"))]
                )
                yield SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content='NCODE_HANDOFF: {"next_mode":"plan"}'))]
                )

        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        visible_chunks = []

        message = stream_assistant_response(
            client,
            {"model": "test", "messages": []},
            on_visible_text=visible_chunks.append,
        )

        self.assertEqual(message["content"], 'Hello\nLEARNCODE_HANDOFF: {"next_mode":"plan"}')
        self.assertEqual("".join(visible_chunks), "Hello\n")

    def test_auto_prompts_cover_plan_and_build_only(self):
        self.assertEqual(AUTO_PROMPTS, {"plan": "Build Plan", "build": "Start Building"})
        self.assertIsNone(AUTO_PROMPTS.get("critic"))

    def test_plan_auto_prompt_skips_without_project_brief(self):
        class FakeCompletions:
            def create(self, **kwargs):
                raise AssertionError("auto prompt should not call the model")

        state = SessionState(mode="plan")
        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        run_auto_prompt_for_mode(client, "test", state)

        self.assertEqual(state.messages_by_mode["plan"], [])

    def test_build_auto_prompt_skips_without_approved_plan(self):
        class FakeCompletions:
            def create(self, **kwargs):
                raise AssertionError("auto prompt should not call the model")

        state = SessionState(mode="build")
        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))

        run_auto_prompt_for_mode(client, "test", state)

        self.assertEqual(state.messages_by_mode["build"], [])

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
    def test_colorize_can_be_disabled_for_plain_output(self):
        self.assertEqual(colorize("hello", "\033[32m", enable=False), "hello")

    def test_render_terminal_markdown_converts_basic_markdown(self):
        rendered_output = render_terminal_markdown("**Important**")

        self.assertIn("Important", rendered_output)
        self.assertNotIn("**Important**", rendered_output)

    def test_color_unified_diff_uses_backgrounds_for_changed_lines(self):
        rendered_output = color_unified_diff("-old\n+new\n unchanged\n", enable=True)

        self.assertIn(BG_RED, rendered_output)
        self.assertIn(BG_GREEN, rendered_output)

    def test_learncode_banner_is_five_lines(self):
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

    def test_tool_event_lines_style_diff_changes_with_background_classes(self):
        lines = tool_event_lines("--- a/main.py\n+++ b/main.py\n-old\n+new")

        self.assertEqual(lines[0], [("class:tool", "● "), ("class:tool", "--- a/main.py")])
        self.assertEqual(lines[1], [("class:tool", "+++ b/main.py")])
        self.assertEqual(lines[2], [("class:diff-del", "-old")])
        self.assertEqual(lines[3], [("class:diff-add", "+new")])

    def test_status_line_shows_mode_and_shortcut_hint(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState(mode="plan"))

        rendered_status = "".join(text for _, text in ui.render_status())

        self.assertIn("plan mode on", rendered_status)
        self.assertIn("pgup/pgdn or wheel scroll", rendered_status)
        self.assertIn("shift+tab mode", rendered_status)

    def test_transcript_renderer_keeps_full_history_for_scrollback(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events = [
            TranscriptEvent("assistant", "First response"),
            TranscriptEvent("user", "Second message"),
        ]

        rendered_transcript = fragment_text(ui.render_transcript())

        self.assertIn("First response", rendered_transcript)
        self.assertIn("Second message", rendered_transcript)

    def test_transcript_cursor_tracks_last_rendered_line(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events.extend(
            TranscriptEvent("assistant", f"Response {index}")
            for index in range(30)
        )

        line_count = len(ui.visible_transcript_lines(width=80))
        cursor_position = ui.transcript_cursor_position()

        self.assertEqual(cursor_position.y, line_count - 1)

    def test_transcript_can_scroll_up_from_live_bottom(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events.extend(
            TranscriptEvent("assistant", f"Response {index}")
            for index in range(30)
        )

        ui.scroll_transcript(5)

        self.assertEqual(ui.transcript_scroll_offset, 5)
        self.assertEqual(ui.transcript_vertical_scroll(SimpleNamespace()), 0)

    def test_transcript_viewport_shows_earliest_event_at_top_scroll(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events = [
            TranscriptEvent("assistant", "First response"),
            *[
                TranscriptEvent("assistant", f"Response {index}")
                for index in range(10)
            ],
        ]

        terminal_size = os.terminal_size((80, 8))
        with patch("learncode_agent.tui.shutil.get_terminal_size", return_value=terminal_size):
            ui.scroll_transcript_to_top()
            rendered_transcript = fragment_text(ui.render_transcript())

        self.assertIn("First response", rendered_transcript)
        self.assertNotIn("Response 9", rendered_transcript)

    def test_transcript_scroll_is_capped_to_available_history(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events.extend(
            TranscriptEvent("assistant", f"Response {index}")
            for index in range(5)
        )

        ui.scroll_transcript(10_000)

        self.assertEqual(
            ui.transcript_scroll_offset,
            ui.max_transcript_scroll_offset(),
        )

    def test_transcript_scroll_reclamps_after_viewport_height_changes(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events = [
            TranscriptEvent("assistant", f"Response {index}")
            for index in range(20)
        ]

        short_terminal = os.terminal_size((80, 8))
        tall_terminal = os.terminal_size((80, 18))
        with patch("learncode_agent.tui.shutil.get_terminal_size", return_value=short_terminal):
            ui.scroll_transcript_to_top()
            self.assertEqual(ui.transcript_scroll_offset, 16)

        with patch("learncode_agent.tui.shutil.get_terminal_size", return_value=tall_terminal):
            ui.visible_transcript_lines(width=80)
            self.assertEqual(ui.transcript_scroll_offset, 6)

    def test_page_scroll_helpers_use_transcript_offset(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events = [
            TranscriptEvent("assistant", f"Response {index}")
            for index in range(20)
        ]

        terminal_size = os.terminal_size((80, 8))
        with patch("learncode_agent.tui.shutil.get_terminal_size", return_value=terminal_size):
            ui.scroll_transcript_page_up()
            self.assertEqual(ui.transcript_scroll_offset, 3)

            ui.scroll_transcript_page_down()
            self.assertEqual(ui.transcript_scroll_offset, 0)

    def test_mouse_wheel_scrolls_transcript_offset(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events = [
            TranscriptEvent("assistant", f"Response {index}")
            for index in range(20)
        ]
        mouse_event = MouseEvent(
            position=Point(x=0, y=0),
            event_type=MouseEventType.SCROLL_UP,
            button=MouseButton.NONE,
            modifiers=frozenset(),
        )

        terminal_size = os.terminal_size((80, 8))
        with patch("learncode_agent.tui.shutil.get_terminal_size", return_value=terminal_size):
            ui.handle_transcript_mouse(mouse_event)

        self.assertEqual(ui.transcript_scroll_offset, 3)

    def test_status_line_shows_scrolled_transcript_state(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.transcript_scroll_offset = 3

        rendered_status = "".join(text for _, text in ui.render_status())

        self.assertIn("scrolled 3 lines", rendered_status)

    def test_tui_starts_with_banner_before_first_user_message(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.events.append(TranscriptEvent("user", "First message"))

        rendered_transcript = fragment_text(ui.render_transcript())

        self.assertLess(
            rendered_transcript.index(" _      _____"),
            rendered_transcript.index("First message"),
        )

    def test_application_uses_full_screen_app_managed_scrollback(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())

        app = ui.build_application(DummyInput(), DummyOutput())

        self.assertTrue(app.full_screen)
        self.assertTrue(app.mouse_support())

    def test_tool_preview_flushes_before_approval_request(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        saw_preview_before_approval = False

        def fake_tool():
            print("Preview")
            return input("Approve? [y/N]: ")

        def fake_request_approval(prompt):
            nonlocal saw_preview_before_approval
            saw_preview_before_approval = any(event.text == "Preview" for event in ui.events)
            return "y"

        ui.request_approval = fake_request_approval

        result = ui.execute_tool(fake_tool, {})

        self.assertEqual(result, "y")
        self.assertTrue(saw_preview_before_approval)

    def test_approval_request_includes_pending_tool_summary(self):
        ui = TerminalAgentTui(SimpleNamespace(), "test", SessionState())
        ui.tool_call("run_bash_command", {"command": "python -m unittest"})

        def fake_tool():
            return input("Approve? [y/N]: ")

        def submit_approval():
            ui.submit_approval("y")

        original_request_approval = ui.request_approval

        def request_and_submit(prompt):
            result = original_request_approval(prompt)
            return result

        ui.request_approval = request_and_submit
        timer = threading.Timer(0.01, submit_approval)
        timer.start()
        result = ui.execute_tool(fake_tool, {})
        timer.join()

        approval_events = [
            event.text
            for event in ui.events
            if "Type y and press Enter to approve." in event.text
        ]
        self.assertEqual(result, "y")
        self.assertTrue(approval_events)
        self.assertIn("Pending Tool call run_bash_command", approval_events[-1])
        self.assertIn("python -m unittest", approval_events[-1])

    def test_tool_call_summary_abbreviates_long_string_arguments(self):
        summary = format_tool_call_summary("write_file", {
            "path": "app.py",
            "content": "x" * 300,
        })

        self.assertIn("Tool call write_file", summary)
        self.assertIn("app.py", summary)
        self.assertIn("[300 chars total]", summary)


if __name__ == "__main__":
    unittest.main()
