from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class AgentRelayCliTests(TestCase):
    def run_cli(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PYTHONPATH": str(ROOT / "src"),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def test_start_creates_session_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = self.run_cli(
                "start",
                "--agent",
                "claude",
                "--task",
                "Draft the handoff schema",
                "--repo",
                tmpdir,
            )
            session_id = result.stdout.splitlines()[0].split()[-1]
            state_path = Path(tmpdir) / ".agent-relay" / "sessions" / session_id / "state.json"
            self.assertTrue(state_path.exists())

            state = json.loads(state_path.read_text())
            self.assertEqual(state["current_agent"], "claude")
            self.assertEqual(state["objective"], "Draft the handoff schema")

    def test_failover_writes_codex_resume_packet_and_launch_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start = self.run_cli(
                "start",
                "--agent",
                "claude",
                "--task",
                "Draft the handoff schema",
                "--repo",
                tmpdir,
            )
            session_id = start.stdout.splitlines()[0].split()[-1]

            self.run_cli(
                "checkpoint",
                session_id,
                "--next-action",
                "Render a Codex resume packet",
                "--decision",
                "Use repo-local state",
                "--touched-file",
                "src/agent_relay/cli.py",
                "--repo",
                tmpdir,
            )

            result = self.run_cli(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "claude rate limit reached",
                "--repo",
                tmpdir,
            )

            resume_path = repo_root / ".agent-relay" / "sessions" / session_id / "resume" / "codex.md"
            state_path = repo_root / ".agent-relay" / "sessions" / session_id / "state.json"

            self.assertTrue(resume_path.exists())

            resume_packet = resume_path.read_text()
            self.assertIn("# Codex Resume Packet", resume_packet)
            self.assertIn("Execution brief:", resume_packet)
            self.assertIn("Files to inspect first:", resume_packet)
            self.assertNotIn("# Claude Code Resume Packet", resume_packet)

            state = json.loads(state_path.read_text())
            handoff = state["handoffs"][0]
            self.assertEqual(state["current_status"], "handoff_prepared")
            self.assertEqual(handoff["to_agent"], "codex")
            self.assertEqual(handoff["launch_status"], "ready")
            self.assertEqual(handoff["launch_profile"], "Codex")
            self.assertEqual(handoff["launch_template_source"], "default")
            self.assertEqual(handoff["launch_command"], f"cd {shlex.quote(str(repo_root))} && codex")
            self.assertIn(str(resume_path), handoff["launch_instructions"])
            self.assertEqual(result.stdout.splitlines()[-1], handoff["launch_command"])

    def test_failover_uses_profile_specific_launch_template_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start = self.run_cli(
                "start",
                "--agent",
                "codex",
                "--task",
                "Hand work back to Claude Code",
                "--repo",
                tmpdir,
            )
            session_id = start.stdout.splitlines()[0].split()[-1]

            self.run_cli(
                "checkpoint",
                session_id,
                "--next-action",
                "Use the Claude Code resume packet",
                "--repo",
                tmpdir,
            )

            result = self.run_cli(
                "failover",
                session_id,
                "--to-agent",
                "claude",
                "--reason",
                "switching agents for review",
                "--repo",
                tmpdir,
                extra_env={
                    "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": "cd {repo_root} && {agent_cli} --resume {resume_path}",
                },
            )

            resume_path = repo_root / ".agent-relay" / "sessions" / session_id / "resume" / "claude.md"
            state_path = repo_root / ".agent-relay" / "sessions" / session_id / "state.json"

            resume_packet = resume_path.read_text()
            self.assertIn("# Claude Code Resume Packet", resume_packet)
            self.assertIn("Priority for this turn:", resume_packet)
            self.assertNotIn("# Codex Resume Packet", resume_packet)

            state = json.loads(state_path.read_text())
            handoff = state["handoffs"][0]
            expected_command = (
                f"cd {shlex.quote(str(repo_root))} && claude --resume {shlex.quote(str(resume_path))}"
            )
            self.assertEqual(handoff["launch_template_source"], "env")
            self.assertEqual(handoff["launch_command"], expected_command)
            self.assertEqual(result.stdout.splitlines()[-1], expected_command)
