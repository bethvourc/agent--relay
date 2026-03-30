"""Tests for concurrent agent execution."""
from __future__ import annotations

from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from agent_relay.concurrent import (
    AgentOutcome,
    ConcurrentResult,
    _build_concurrent_prompt,
    _build_shell_command,
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
        self.assertIn("CONVERSATION_COMPLETE", prompt)

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
        cmd = _build_shell_command("claude", Path("/tmp/prompt.md"), Path("/tmp/repo"))
        self.assertIn("claude", cmd)
        self.assertIn("-p", cmd)
        # Should NOT have stream-json in tmux mode (interactive)
        self.assertNotIn("stream-json", cmd)

    def test_codex_command_interactive(self) -> None:
        cmd = _build_shell_command("codex", Path("/tmp/prompt.md"), Path("/tmp/repo"))
        self.assertIn("codex", cmd)
        # Should NOT have --json in tmux mode
        self.assertNotIn("--json", cmd)


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
