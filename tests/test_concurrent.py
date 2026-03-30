"""Tests for concurrent agent execution."""
from __future__ import annotations

import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent_relay.concurrent import (
    AgentOutcome,
    ConcurrentControl,
    ConcurrentResult,
    _build_concurrent_prompt,
    _build_shell_command,
    parse_concurrent_control,
    run_concurrent,
    _require_tmux,
)


class BuildConcurrentPromptTests(TestCase):
    def test_includes_task_and_agent_info(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Fix all tests",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/repo/.agent-relay/sessions/abc/workspace-log.md"),
            tmux_session="relay-abc",
        )
        self.assertIn("Fix all tests", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("CONCURRENT", prompt)
        self.assertIn("workspace-log.md", prompt)
        self.assertIn("pane 0", prompt)

    def test_lists_other_agents_with_pane_numbers(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Collaborate",
            slot=1,
            agent_key="codex",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            tmux_session="relay-test",
        )
        self.assertIn("Pane 0", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("tmux capture-pane", prompt)
        self.assertIn("relay-test", prompt)

    def test_includes_completion_marker(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Do work",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            tmux_session="relay-test",
        )
        self.assertIn("RELAY_STATUS:", prompt)
        self.assertIn("continue, blocked, done, error", prompt)

    def test_tmux_capture_instructions_per_pane(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Team task",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex", "claude"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            tmux_session="relay-xyz",
        )
        # Pane 0 (claude) should see instructions for pane 1 and pane 2
        self.assertIn("relay-xyz:0.1", prompt)
        self.assertIn("relay-xyz:0.2", prompt)


class BuildShellCommandTests(TestCase):
    def test_claude_command_interactive(self) -> None:
        cmd = _build_shell_command("claude", Path("/tmp/prompt.md"), Path("/tmp/repo"), Path("/tmp/exit-code.txt"))
        self.assertIn("claude", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("exit-code.txt", cmd)
        # Should NOT have stream-json in tmux mode (interactive)
        self.assertNotIn("stream-json", cmd)

    def test_codex_command_interactive(self) -> None:
        cmd = _build_shell_command("codex", Path("/tmp/prompt.md"), Path("/tmp/repo"), Path("/tmp/exit-code.txt"))
        self.assertIn("codex", cmd)
        # Should NOT have --json in tmux mode
        self.assertNotIn("--json", cmd)


class ParseConcurrentControlTests(TestCase):
    def test_parses_structured_status(self) -> None:
        control = parse_concurrent_control(
            'Notes\nRELAY_STATUS: {"status":"done","reason":"Finished","remaining_work":[],"verification":["pytest"]}'
        )
        self.assertEqual(
            control,
            ConcurrentControl(
                status="done",
                reason="Finished",
                remaining_work=(),
                verification=("pytest",),
            ),
        )

    def test_legacy_marker_requires_standalone_line(self) -> None:
        control = parse_concurrent_control("Notes\nCONVERSATION_COMPLETE")
        self.assertEqual(control.status, "done")

    def test_inline_marker_is_not_a_done_signal(self) -> None:
        control = parse_concurrent_control("I mentioned CONVERSATION_COMPLETE in my notes.")
        self.assertEqual(control.status, "continue")


class RequireTmuxTests(TestCase):
    @patch("shutil.which", return_value="/usr/bin/tmux")
    def test_passes_when_tmux_available(self, mock_which) -> None:
        path = _require_tmux()
        self.assertEqual(path, "/usr/bin/tmux")

    @patch("shutil.which", return_value=None)
    def test_raises_when_tmux_missing(self, mock_which) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _require_tmux()
        self.assertIn("tmux is required", str(ctx.exception))


class AgentOutcomeTests(TestCase):
    def test_dataclass_creation(self) -> None:
        outcome = AgentOutcome(
            slot=0,
            agent_key="claude",
            exit_code=0,
            raw_stdout="output",
            raw_stderr="",
            text="normalized",
            summary="Did stuff.",
            done_signal=True,
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
        )
        self.assertEqual(outcome.slot, 0)
        self.assertEqual(outcome.agent_key, "claude")
        self.assertTrue(outcome.done_signal)

    def test_none_exit_code_for_killed(self) -> None:
        outcome = AgentOutcome(
            slot=0, agent_key="claude", exit_code=None,
            raw_stdout="", raw_stderr="", text="", summary="",
            done_signal=False, started_at="", finished_at="",
        )
        self.assertIsNone(outcome.exit_code)


class ConcurrentResultTests(TestCase):
    def test_dataclass_creation(self) -> None:
        result = ConcurrentResult(
            session_id="test-123",
            agents=("claude", "codex"),
            stop_reason="all_done",
            elapsed_seconds=42.5,
            outcomes=(),
        )
        self.assertEqual(result.session_id, "test-123")
        self.assertEqual(result.stop_reason, "all_done")
        self.assertEqual(result.elapsed_seconds, 42.5)
        self.assertEqual(result.agents, ("claude", "codex"))


class RunConcurrentTests(TestCase):
    def _run(
        self,
        *,
        pane_contents: dict[int, str],
        exit_codes: dict[int, int | None],
        pane_dead,
        session_exists,
        isatty: bool = False,
        max_time_seconds: int = 600,
    ) -> ConcurrentResult:
        with TemporaryDirectory() as tmpdir:
            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session"
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                side_effect=session_exists,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                side_effect=pane_dead,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda _session, slot: pane_contents.get(slot, ""),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                side_effect=lambda path: exit_codes.get(int(path.parent.name.split("-")[-1]), None),
            ), patch(
                "os.isatty",
                return_value=isatty,
            ):
                return run_concurrent(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_time_seconds=max_time_seconds,
                )

    def test_all_done_requires_done_status_from_all_panes(self) -> None:
        result = self._run(
            pane_contents={
                0: 'Agent 0 finished\nRELAY_STATUS: {"status":"done","reason":"Complete","remaining_work":[],"verification":["review"]}',
                1: 'Agent 1 finished\nRELAY_STATUS: {"status":"done","reason":"Complete","remaining_work":[],"verification":["tests"]}',
            },
            exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "all_done")
        self.assertTrue(all(outcome.done_signal for outcome in result.outcomes))
        self.assertEqual([outcome.control_status for outcome in result.outcomes], ["done", "done"])

    def test_start_session_uses_schema_valid_workstream_kind(self) -> None:
        start_session_mock = MagicMock()
        with TemporaryDirectory() as tmpdir:
            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session",
                start_session_mock,
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda _session, _slot: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":[]}',
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=0,
            ), patch(
                "os.isatty",
                return_value=False,
            ):
                run_concurrent(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                )

        self.assertEqual(start_session_mock.call_args.kwargs["workstream_kind"], "mixed")

    def test_clean_exit_without_done_is_incomplete(self) -> None:
        result = self._run(
            pane_contents={
                0: 'Still work left\nRELAY_STATUS: {"status":"continue","reason":"Docs pending","remaining_work":["docs"],"verification":[]}',
                1: 'Finished my part\nRELAY_STATUS: {"status":"done","reason":"Code ready","remaining_work":[],"verification":["tests"]}',
            },
            exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "incomplete")
        self.assertEqual([outcome.control_status for outcome in result.outcomes], ["continue", "done"])

    def test_nonzero_exit_is_agent_error(self) -> None:
        result = self._run(
            pane_contents={
                0: 'Build failed\nRELAY_STATUS: {"status":"error","reason":"Tests failed","remaining_work":["fix tests"],"verification":["pytest failed"]}',
                1: 'Finished my part\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":["review"]}',
            },
            exit_codes={0: 1, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "agent_error")
        self.assertEqual(result.outcomes[0].exit_code, 1)
        self.assertEqual(result.outcomes[0].control_status, "error")

    def test_killed_tmux_session_is_interrupted(self) -> None:
        result = self._run(
            pane_contents={},
            exit_codes={},
            pane_dead=lambda _session, _slot: False,
            session_exists=lambda _session: False,
        )
        self.assertEqual(result.stop_reason, "interrupted")
        self.assertTrue(all(outcome.exit_code is None for outcome in result.outcomes))

    def test_timeout_is_enforced_while_attached(self) -> None:
        attach_proc = MagicMock()
        attach_proc.poll.return_value = None
        attach_proc.wait.return_value = 0
        with TemporaryDirectory() as tmpdir:
            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session"
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                return_value=False,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda _session, slot: (
                    'Still running\nRELAY_STATUS: {"status":"continue","reason":"Work in progress","remaining_work":["finish"],"verification":[]}'
                    if slot == 0
                    else ""
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=None,
            ), patch(
                "os.isatty",
                return_value=True,
            ), patch(
                "subprocess.Popen",
                return_value=attach_proc,
            ) as popen_mock, patch(
                "time.time",
                return_value=10**20,
            ):
                result = run_concurrent(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_time_seconds=1,
                )
        self.assertEqual(result.stop_reason, "max_time")
        popen_mock.assert_called_once()

    def test_inline_done_marker_text_does_not_count(self) -> None:
        result = self._run(
            pane_contents={
                0: 'I documented the string CONVERSATION_COMPLETE for later.\nRELAY_STATUS: {"status":"continue","reason":"Not finished","remaining_work":["more work"],"verification":[]}',
                1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":["review"]}',
            },
            exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "incomplete")
        self.assertEqual(result.outcomes[0].control_status, "continue")
