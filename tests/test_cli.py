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

    def test_start_creates_initial_state_checkpoint_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
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
            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            state_path = session_root / "state.json"
            summary = session_root / "summary.md"
            checkpoints = list((session_root / "checkpoints").glob("*.json"))

            self.assertTrue(state_path.exists())
            self.assertTrue(summary.exists())
            self.assertEqual(len(checkpoints), 1)

            state = json.loads(state_path.read_text())
            self.assertEqual(state["current_agent"], "claude")
            self.assertEqual(state["objective"], "Draft the handoff schema")
            self.assertEqual(state["current_status"], "active")
            self.assertEqual(state["latest_checkpoint_id"], checkpoints[0].stem)
            self.assertIn("Latest checkpoint:", summary.read_text())

    def test_checkpoint_creates_new_checkpoint_and_updates_summary(self) -> None:
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
            session_root = repo_root / ".agent-relay" / "sessions" / session_id

            result = self.run_cli(
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

            checkpoints = list((session_root / "checkpoints").glob("*.json"))
            self.assertEqual(len(checkpoints), 2)
            latest_checkpoint_id = result.stdout.splitlines()[-1]
            self.assertIn(latest_checkpoint_id, {path.stem for path in checkpoints})

            state = json.loads((session_root / "state.json").read_text())
            summary = (session_root / "summary.md").read_text()

            self.assertEqual(state["latest_checkpoint_id"], latest_checkpoint_id)
            self.assertIn("Render a Codex resume packet", summary)
            self.assertIn("Use repo-local state", summary)
            self.assertIn("src/agent_relay/cli.py", summary)

    def test_failover_writes_resume_packet_and_records_checkpoint_reference(self) -> None:
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

            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            resume_path = session_root / "resume" / "codex.md"
            state = json.loads((session_root / "state.json").read_text())
            handoff = state["handoffs"][0]

            self.assertTrue(resume_path.exists())
            self.assertEqual(state["current_status"], "handoff_prepared")
            self.assertEqual(handoff["to_agent"], "codex")
            self.assertEqual(handoff["checkpoint_id"], state["latest_checkpoint_id"])
            self.assertIn("# Codex Resume Packet", resume_path.read_text())
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
            state = json.loads((repo_root / ".agent-relay" / "sessions" / session_id / "state.json").read_text())
            handoff = state["handoffs"][0]
            expected_command = (
                f"cd {shlex.quote(str(repo_root))} && claude --resume {shlex.quote(str(resume_path))}"
            )

            self.assertIn("# Claude Code Resume Packet", resume_path.read_text())
            self.assertEqual(handoff["launch_template_source"], "env")
            self.assertEqual(handoff["launch_command"], expected_command)
            self.assertEqual(result.stdout.splitlines()[-1], expected_command)

    def test_launch_dry_run_prints_latest_handoff_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start = self.run_cli(
                "start",
                "--agent",
                "claude",
                "--task",
                "Prepare a dry-run launch",
                "--repo",
                tmpdir,
            )
            session_id = start.stdout.splitlines()[0].split()[-1]

            self.run_cli(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "ready to hand off",
                "--repo",
                tmpdir,
            )

            result = self.run_cli(
                "launch",
                session_id,
                "--repo",
                tmpdir,
            )

            state = json.loads((repo_root / ".agent-relay" / "sessions" / session_id / "state.json").read_text())
            handoff = state["handoffs"][0]

            self.assertIn("Launch target: codex", result.stdout)
            self.assertIn(handoff["launch_command"], result.stdout)
            self.assertIn(handoff["launch_instructions"], result.stdout)
            self.assertEqual(handoff["launch_status"], "ready")
            self.assertEqual(state["current_agent"], "claude")

    def test_launch_execute_runs_command_and_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start = self.run_cli(
                "start",
                "--agent",
                "claude",
                "--task",
                "Launch the next agent",
                "--repo",
                tmpdir,
            )
            session_id = start.stdout.splitlines()[0].split()[-1]

            launch_template = (
                f"cd {{repo_root}} && {shlex.quote(sys.executable)} -c "
                "'from pathlib import Path; Path(\"launch-marker.txt\").write_text(\"ok\")'"
            )
            self.run_cli(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "ready to execute handoff",
                "--repo",
                tmpdir,
                extra_env={
                    "AGENT_RELAY_CODEX_LAUNCH_TEMPLATE": launch_template,
                },
            )

            result = self.run_cli(
                "launch",
                session_id,
                "--repo",
                tmpdir,
                "--execute",
            )

            state = json.loads((repo_root / ".agent-relay" / "sessions" / session_id / "state.json").read_text())
            handoff = state["handoffs"][0]
            marker_path = repo_root / "launch-marker.txt"

            self.assertEqual(result.returncode, 0)
            self.assertTrue(marker_path.exists())
            self.assertEqual(marker_path.read_text(), "ok")
            self.assertEqual(handoff["launch_status"], "succeeded")
            self.assertEqual(handoff["exit_code"], 0)
            self.assertEqual(state["current_agent"], "codex")
            self.assertEqual(state["current_status"], "active")
