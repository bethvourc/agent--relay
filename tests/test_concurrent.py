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
            pane_snapshot_paths=[Path("/tmp/repo/pane-0.txt"), Path("/tmp/repo/pane-1.txt")],
        )
        self.assertIn("Fix all tests", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("CONCURRENT", prompt)
        self.assertIn("workspace-log.md", prompt)
        self.assertIn("slot 0", prompt.lower())
        self.assertIn("There is no interactive approval loop in concurrent mode", prompt)

    def test_lists_other_agents_with_slot_numbers(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Collaborate",
            slot=1,
            agent_key="codex",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[Path("/tmp/pane-0.txt"), Path("/tmp/pane-1.txt")],
        )
        self.assertIn("Slot 0", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("/tmp/pane-0.txt", prompt)
        self.assertNotIn("tmux capture-pane", prompt)

    def test_includes_completion_marker(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Do work",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[Path("/tmp/pane-0.txt"), Path("/tmp/pane-1.txt")],
        )
        self.assertIn("RELAY_STATUS:", prompt)
        self.assertIn("continue, blocked, done, error", prompt)

    def test_snapshot_instructions_per_slot(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Team task",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex", "claude"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[
                Path("/tmp/pane-0.txt"),
                Path("/tmp/pane-1.txt"),
                Path("/tmp/pane-2.txt"),
            ],
        )
        self.assertIn("/tmp/pane-1.txt", prompt)
        self.assertIn("/tmp/pane-2.txt", prompt)

    def test_includes_continuation_context_when_requested(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Follow up on the prior review",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[Path("/tmp/pane-0.txt"), Path("/tmp/pane-1.txt")],
            continued_from_session_id="20260330-abc123",
            continued_workspace_log=Path("/tmp/repo/.agent-relay/sessions/20260330-abc123/workspace-log.md"),
            continued_session_root=Path("/tmp/repo/.agent-relay/sessions/20260330-abc123"),
        )
        self.assertIn("## Continuation Context", prompt)
        self.assertIn("20260330-abc123", prompt)
        self.assertIn("Do not restart from scratch", prompt)


class BuildShellCommandTests(TestCase):
    def test_claude_command_interactive(self) -> None:
        cmd = _build_shell_command("claude", Path("/tmp/prompt.md"), Path("/tmp/repo"), Path("/tmp/exit-code.txt"))
        self.assertIn("claude", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("--permission-mode dontAsk", cmd)
        self.assertIn("exit-code.txt", cmd)
        # Should NOT have stream-json in tmux mode (interactive)
        self.assertNotIn("stream-json", cmd)

    def test_codex_command_interactive(self) -> None:
        cmd = _build_shell_command("codex", Path("/tmp/prompt.md"), Path("/tmp/repo"), Path("/tmp/exit-code.txt"))
        self.assertIn("codex", cmd)
        self.assertIn("-a never", cmd)
        self.assertIn("-s workspace-write", cmd)
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
            tmux_session="relay-test-00",
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
        self.assertEqual(outcome.tmux_session, "relay-test-00")
        self.assertTrue(outcome.done_signal)

    def test_none_exit_code_for_killed(self) -> None:
        outcome = AgentOutcome(
            slot=0, agent_key="claude", tmux_session="relay-test-00", exit_code=None,
            raw_stdout="", raw_stderr="", text="", summary="",
            done_signal=False, started_at="", finished_at="",
        )
        self.assertIsNone(outcome.exit_code)


class ConcurrentResultTests(TestCase):
    def test_dataclass_creation(self) -> None:
        result = ConcurrentResult(
            session_id="test-123",
            agents=("claude", "codex"),
            tmux_sessions=("relay-test-00", "relay-test-01"),
            continued_from_session_id=None,
            stop_reason="all_done",
            elapsed_seconds=42.5,
            outcomes=(),
        )
        self.assertEqual(result.session_id, "test-123")
        self.assertEqual(result.stop_reason, "all_done")
        self.assertEqual(result.elapsed_seconds, 42.5)
        self.assertEqual(result.agents, ("claude", "codex"))
        self.assertEqual(result.tmux_sessions, ("relay-test-00", "relay-test-01"))


class RunConcurrentTests(TestCase):
    def _run(
        self,
        *,
        pane_contents: dict[int, str],
        exit_codes: dict[int, int | None],
        pane_dead,
        session_exists,
        max_time_seconds: int = 600,
        on_agent_start=None,
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
                side_effect=lambda session_name, _slot: pane_contents.get(int(session_name.rsplit("-", 1)[-1]), ""),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                side_effect=lambda path: exit_codes.get(int(path.parent.name.split("-")[-1]), None),
            ):
                return run_concurrent(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_time_seconds=max_time_seconds,
                    on_agent_start=on_agent_start,
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
        self.assertEqual(
            [outcome.tmux_session for outcome in result.outcomes],
            [f"relay-{result.session_id}-00", f"relay-{result.session_id}-01"],
        )

    def test_start_session_uses_schema_valid_workstream_kind_and_separate_tmux_sessions(self) -> None:
        start_session_mock = MagicMock()
        tmux_mock = MagicMock(return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""))
        with TemporaryDirectory() as tmpdir:
            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session",
                start_session_mock,
            ), patch(
                "agent_relay.concurrent._tmux",
                tmux_mock,
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
            ):
                run_concurrent(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                )

        self.assertEqual(start_session_mock.call_args.kwargs["workstream_kind"], "mixed")
        session_id = start_session_mock.call_args.kwargs["session_id"]
        new_session_calls = [
            call.args
            for call in tmux_mock.call_args_list
            if call.args and call.args[0] == "new-session"
        ]
        self.assertEqual(len(new_session_calls), 2)
        self.assertIn(
            ("set-option", "-t", f"relay-{session_id}-00", "mouse", "on"),
            [call.args[:5] for call in tmux_mock.call_args_list if len(call.args) >= 5],
        )
        self.assertIn(
            ("set-option", "-t", f"relay-{session_id}-01", "mouse", "on"),
            [call.args[:5] for call in tmux_mock.call_args_list if len(call.args) >= 5],
        )

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

    def test_timeout_is_enforced_without_attached_tmux_client(self) -> None:
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
                side_effect=lambda session_name, _slot: (
                    'Still running\nRELAY_STATUS: {"status":"continue","reason":"Work in progress","remaining_work":["finish"],"verification":[]}'
                    if int(session_name.rsplit("-", 1)[-1]) == 0
                    else ""
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=None,
            ), patch(
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

    def test_pane_snapshots_are_written_locally(self) -> None:
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
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda session_name, _slot: f"slot {int(session_name.rsplit('-', 1)[-1])} snapshot",
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=0,
            ):
                result = run_concurrent(
                    Path(tmpdir),
                    agents=["claude", "codex"],
                    task="Finish the task",
                )

                session_dir = Path(tmpdir) / ".agent-relay" / "sessions" / result.session_id / "concurrent"
                self.assertEqual(
                    (session_dir / "agent-00" / "pane.txt").read_text(encoding="utf-8"),
                    "slot 0 snapshot",
                )
                self.assertEqual(
                    (session_dir / "agent-01" / "pane.txt").read_text(encoding="utf-8"),
                    "slot 1 snapshot",
                )

    def test_on_agent_start_receives_attachable_tmux_session(self) -> None:
        started: list[tuple[int, str, str]] = []
        result = self._run(
            pane_contents={
                0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":[]}',
                1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":[]}',
            },
            exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
            on_agent_start=lambda slot, agent_key, tmux_session: started.append((slot, agent_key, tmux_session)),
        )
        self.assertEqual(
            started,
            [
                (0, "claude", f"relay-{result.session_id}-00"),
                (1, "codex", f"relay-{result.session_id}-01"),
            ],
        )

    def test_continue_from_missing_session_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ):
                with self.assertRaises(SystemExit) as ctx:
                    run_concurrent(
                        Path(tmpdir),
                        agents=["claude", "codex"],
                        task="Continue missing work",
                        continue_from_session_id="missing-session",
                    )
        self.assertIn("Session not found: missing-session", str(ctx.exception))
