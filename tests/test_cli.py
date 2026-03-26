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

    def run_cli_json(self, *args: str, extra_env: dict[str, str] | None = None) -> dict:
        result = self.run_cli("--json", *args, extra_env=extra_env)
        return json.loads(result.stdout)

    def start_snapshot_session(
        self,
        tmpdir: str,
        *,
        agent: str,
        task: str,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        return self.run_cli_json(
            "start",
            "--agent",
            agent,
            "--task",
            task,
            "--snapshot-mode",
            "full",
            "--repo",
            tmpdir,
            extra_env=extra_env,
        )

    def test_start_creates_initial_state_checkpoint_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            data = self.start_snapshot_session(tmpdir, agent="claude", task="Draft the handoff schema")
            session_id = data["session_id"]
            self.assertEqual(data["command"], "start")
            self.assertEqual(data["agent"], "claude")
            self.assertEqual(data["status"], "active")
            self.assertEqual(data["storage_model"], "journal_v2")

            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            manifest_path = session_root / "session.json"
            journal_events = list((session_root / "journal").glob("*.json"))
            checkpoint_root = session_root / "objects" / "checkpoints" / data["checkpoint_id"]
            summary = checkpoint_root / "summary.md"

            self.assertTrue(manifest_path.exists())
            self.assertTrue(summary.exists())
            self.assertEqual(len(journal_events), 2)

            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            self.assertEqual(inspect["current_agent"], "claude")
            self.assertEqual(inspect["objective"], "Draft the handoff schema")
            self.assertEqual(inspect["current_status"], "active")
            self.assertEqual(inspect["latest_checkpoint_id"], data["checkpoint_id"])
            self.assertIn("Checkpoint ID:", summary.read_text())

    def test_checkpoint_creates_new_checkpoint_and_updates_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Draft the handoff schema")
            session_id = start_data["session_id"]
            session_root = repo_root / ".agent-relay" / "sessions" / session_id

            cp_data = self.run_cli_json(
                "checkpoint",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Render a Codex resume packet",
                "--decision",
                "Use repo-local state",
                "--touched-file",
                "src/agent_relay/cli.py",
                "--repo",
                tmpdir,
            )

            checkpoints = list((session_root / "objects" / "checkpoints").glob("*"))
            self.assertEqual(len(checkpoints), 2)
            latest_checkpoint_id = cp_data["checkpoint_id"]
            self.assertEqual(cp_data["command"], "checkpoint")
            self.assertIn(latest_checkpoint_id, {path.name for path in checkpoints})

            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            summary = (session_root / "objects" / "checkpoints" / latest_checkpoint_id / "summary.md").read_text()

            self.assertEqual(inspect["latest_checkpoint_id"], latest_checkpoint_id)
            self.assertIn("Render a Codex resume packet", summary)
            self.assertIn("Use repo-local state", summary)
            self.assertIn("src/agent_relay/cli.py", summary)

    def test_checkpoint_captures_notes_validation_and_git_touched_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            subprocess.run(["git", "init"], cwd=repo_root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo_root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Agent Relay Tests"], cwd=repo_root, text=True, capture_output=True, check=True)

            notes_dir = repo_root / "notes"
            notes_dir.mkdir()
            (notes_dir / "implementation.md").write_text("Added the Phase 6 capture flow\n")
            (notes_dir / "validation.txt").write_text("Manual verification still pending\n")
            (repo_root / "src").mkdir()
            (repo_root / "src" / "phase6.py").write_text("print('phase6')\n")
            subprocess.run(["git", "add", "."], cwd=repo_root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, text=True, capture_output=True, check=True)
            (repo_root / "src" / "phase6.py").write_text("print('phase6 updated')\n", encoding="utf-8")

            start_data = self.run_cli_json(
                "start",
                "--agent",
                "claude",
                "--task",
                "Capture richer checkpoint state",
                "--repo",
                tmpdir,
            )
            session_id = start_data["session_id"]

            cp_data = self.run_cli_json(
                "checkpoint",
                session_id,
                "--next-action",
                "Prepare the final handoff",
                "--research-note",
                "Investigated the last safe checkpoint flow",
                "--implementation-note-file",
                "notes/implementation.md",
                "--validation-status",
                "partial",
                "--validation-summary-file",
                "notes/validation.txt",
                "--capture-git-changes",
                "--repo",
                tmpdir,
            )

            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            summary = (session_root / "objects" / "checkpoints" / cp_data["checkpoint_id"] / "summary.md").read_text()

            self.assertEqual(cp_data["status"], "active")
            self.assertEqual(inspect["validation"]["status"], "partial")
            self.assertEqual(inspect["validation"]["summary"], "Manual verification still pending")
            self.assertIn("Investigated the last safe checkpoint flow", inspect["research_notes"])
            self.assertIn("Added the Phase 6 capture flow", inspect["implementation_notes"])
            self.assertIn("src/phase6.py", inspect["touched_files"])
            self.assertTrue(all(not path.startswith(".agent-relay/") for path in inspect["touched_files"]))
            self.assertIn("Task status:", summary)
            self.assertIn("Added the Phase 6 capture flow", summary)

    def test_failover_writes_resume_packet_and_records_checkpoint_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Draft the handoff schema")
            session_id = start_data["session_id"]

            prepare = self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Render a Codex resume packet",
                "--decision",
                "Use repo-local state",
                "--repo",
                tmpdir,
            )

            fo_data = self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "claude rate limit reached",
                "--repo",
                tmpdir,
            )

            resume_path = Path(fo_data["resume_path"])
            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            handoff = inspect["handoffs"][0]

            self.assertTrue(resume_path.exists())
            self.assertEqual(inspect["current_status"], "ready_for_handoff")
            self.assertEqual(handoff["to_agent"], "codex")
            self.assertEqual(handoff["checkpoint_id"], prepare["checkpoint_id"])
            self.assertIn("# Codex Resume Packet", resume_path.read_text())
            preview = self.run_cli_json("launch", session_id, "--repo", tmpdir)
            self.assertEqual(fo_data["launch_command"], preview["launch_command"])

    def test_failover_uses_profile_specific_launch_template_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="codex", task="Hand work back to Claude Code")
            session_id = start_data["session_id"]

            self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Use the Claude Code resume packet",
                "--repo",
                tmpdir,
            )

            fo_data = self.run_cli_json(
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

            resume_path = Path(fo_data["resume_path"])
            expected_command = (
                f"cd {shlex.quote(str(repo_root))} && claude --resume {shlex.quote(str(resume_path))}"
            )

            self.assertIn("# Claude Code Resume Packet", resume_path.read_text())
            self.assertEqual(fo_data["launch_command"], expected_command)

    def test_launch_dry_run_prints_latest_handoff_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Prepare a dry-run launch")
            session_id = start_data["session_id"]

            self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Ready to hand off",
                "--repo",
                tmpdir,
            )

            failover = self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "ready to hand off",
                "--repo",
                tmpdir,
            )

            launch_data = self.run_cli_json(
                "launch",
                session_id,
                "--repo",
                tmpdir,
            )

            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            handoff = inspect["handoffs"][0]

            self.assertEqual(launch_data["command"], "launch")
            self.assertEqual(launch_data["mode"], "dry_run")
            self.assertEqual(launch_data["session_id"], session_id)
            self.assertEqual(launch_data["target"], "codex")
            self.assertEqual(launch_data["launch_command"], failover["launch_command"])
            self.assertEqual(launch_data["launch_instructions"], failover["launch_instructions"])
            self.assertEqual(handoff["launch_status"], "ready")
            self.assertEqual(inspect["current_agent"], "claude")

    def test_prepare_creates_ready_for_handoff_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Prepare a clean handoff")
            session_id = start_data["session_id"]
            session_root = repo_root / ".agent-relay" / "sessions" / session_id

            prepare_data = self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Hand off to Codex for continuation",
                "--decision",
                "Pause before preparing the target packet",
                "--repo",
                tmpdir,
            )

            checkpoints = list((session_root / "objects" / "checkpoints").glob("*"))
            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            summary = (session_root / "objects" / "checkpoints" / prepare_data["checkpoint_id"] / "summary.md").read_text()

            self.assertEqual(len(checkpoints), 2)
            self.assertEqual(prepare_data["command"], "prepare")
            self.assertEqual(prepare_data["status"], "ready_for_handoff")
            self.assertEqual(inspect["current_status"], "ready_for_handoff")
            self.assertEqual(inspect["next_action"], "Hand off to Codex for continuation")
            self.assertEqual(inspect["latest_checkpoint_id"], prepare_data["checkpoint_id"])
            self.assertIn("Pause before preparing the target packet", inspect["decisions"])
            self.assertIn("Phase: ready_for_handoff", summary)
            self.assertIn("Hand off to Codex for continuation", summary)

    def test_dashboard_lists_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.start_snapshot_session(tmpdir, agent="claude", task="First task")
            self.start_snapshot_session(tmpdir, agent="codex", task="Second task")

            data = self.run_cli_json("dashboard", "--repo", tmpdir)
            self.assertEqual(data["command"], "dashboard")
            self.assertEqual(len(data["sessions"]), 2)
            agents = {s["agent"] for s in data["sessions"]}
            self.assertEqual(agents, {"claude", "codex"})

    def test_dashboard_empty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data = self.run_cli_json("dashboard", "--repo", tmpdir)
            self.assertEqual(data["sessions"], [])

    def test_launch_execute_runs_command_and_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Launch the next agent")
            session_id = start_data["session_id"]

            launch_template = (
                f"cd {{repo_root}} && {shlex.quote(sys.executable)} -c "
                "'from pathlib import Path; Path(\"launch-marker.txt\").write_text(\"ok\")' {resume_path}"
            )
            self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Ready to execute handoff",
                "--repo",
                tmpdir,
            )

            self.run_cli_json(
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
                "--json",
                "launch",
                session_id,
                "--repo",
                tmpdir,
                "--execute",
            )

            launch_data = json.loads(result.stdout)
            marker_path = repo_root / "launch-marker.txt"

            self.assertEqual(result.returncode, 0)
            self.assertTrue(marker_path.exists())
            self.assertEqual(marker_path.read_text(), "ok")
            self.assertFalse(launch_data["ownership_transferred"])

            awaiting = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            self.assertEqual(awaiting["current_agent"], "claude")
            self.assertEqual(awaiting["current_status"], "awaiting_resume")
            self.assertEqual(awaiting["handoffs"][0]["launch_status"], "succeeded")

            resume = self.run_cli_json("resume", session_id, "--repo", tmpdir)
            self.assertEqual(resume["current_agent"], "codex")
            after_resume = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            self.assertEqual(after_resume["current_agent"], "codex")
            self.assertEqual(after_resume["current_status"], "active")
