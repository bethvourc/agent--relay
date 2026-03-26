from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.v2.capture_support import CaptureOptions
from agent_relay.v2.checkpoints import create_checkpoint_for_command
from agent_relay.v2.tx import JournalCommitRequest, SessionTransaction
from tests.v2_fixtures import build_sample_v2_session


class AgentRelayV2HandoffCliTests(TestCase):
    def run_cli(self, *args: str, repo_root: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT / "src"), **(extra_env or {})}
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "relay@example.com"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Relay Test"], cwd=repo_root, check=True, capture_output=True, text=True)
        (repo_root / "src").mkdir()
        (repo_root / "src" / "demo.py").write_text("print('demo')\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/demo.py"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, check=True, capture_output=True, text=True)

    def prepare_session(self, repo_root: Path) -> dict[str, str]:
        self.init_git_repo(repo_root)
        fixture = build_sample_v2_session(repo_root)
        create_checkpoint_for_command(
            repo_root,
            fixture["session_id"],
            command_name="prepare",
            options=CaptureOptions(next_action="Prepare a v2 CLI handoff"),
            owner="test:prepare:cli",
        )
        return fixture

    def test_cli_launch_keeps_ownership_until_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            env = {
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"{shlex_quote(sys.executable)} -c "
                    "\"from pathlib import Path; Path('claude-launch.txt').write_text('ok')\" "
                    "{resume_path}"
                )
            }

            failover = self.run_cli(
                "--json",
                "failover",
                fixture["session_id"],
                "--to-agent",
                "claude",
                "--reason",
                "CLI v2 handoff",
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertEqual(failover.returncode, 0, failover.stderr)
            failover_data = json.loads(failover.stdout)

            launch = self.run_cli(
                "--json",
                "launch",
                fixture["session_id"],
                "--handoff-id",
                failover_data["handoff_id"],
                "--execute",
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertEqual(launch.returncode, 0, launch.stderr)
            launch_data = json.loads(launch.stdout)

            inspect_after_launch = self.run_cli(
                "--json",
                "inspect",
                fixture["session_id"],
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertEqual(inspect_after_launch.returncode, 0, inspect_after_launch.stderr)
            inspect_launch_data = json.loads(inspect_after_launch.stdout)

            resume = self.run_cli(
                "--json",
                "resume",
                fixture["session_id"],
                "--handoff-id",
                failover_data["handoff_id"],
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertEqual(resume.returncode, 0, resume.stderr)
            resume_data = json.loads(resume.stdout)

            inspect_after_resume = self.run_cli(
                "--json",
                "inspect",
                fixture["session_id"],
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertEqual(inspect_after_resume.returncode, 0, inspect_after_resume.stderr)
            inspect_resume_data = json.loads(inspect_after_resume.stdout)

            self.assertTrue(Path(failover_data["resume_path"]).exists())
            self.assertEqual(launch_data["launch_status"], "succeeded")
            self.assertTrue((repo_root / "claude-launch.txt").exists())
            self.assertEqual(inspect_launch_data["current_status"], "awaiting_resume")
            self.assertEqual(inspect_launch_data["current_agent"], "codex")
            self.assertEqual(resume_data["current_agent"], "claude")
            self.assertEqual(inspect_resume_data["current_status"], "active")
            self.assertEqual(inspect_resume_data["current_agent"], "claude")
            self.assertEqual(inspect_resume_data["last_resume_handoff_id"], failover_data["handoff_id"])

    def test_cli_launch_preview_warns_and_execute_refuses_when_template_ignores_packet(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            env = {
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"{shlex_quote(sys.executable)} -c "
                    "\"print('unsafe launch template')\""
                )
            }

            failover = self.run_cli(
                "--json",
                "failover",
                fixture["session_id"],
                "--to-agent",
                "claude",
                "--reason",
                "Unsafe launch template check",
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertEqual(failover.returncode, 0, failover.stderr)
            handoff_id = json.loads(failover.stdout)["handoff_id"]

            preview = self.run_cli(
                "--json",
                "launch",
                fixture["session_id"],
                "--handoff-id",
                handoff_id,
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            preview_data = json.loads(preview.stdout)
            self.assertFalse(preview_data["packet_aware"])
            self.assertEqual(preview_data["execute_policy"], "refuse")
            self.assertIn("does not pass the resume packet", preview_data["warning"])

            execute = self.run_cli(
                "--json",
                "launch",
                fixture["session_id"],
                "--handoff-id",
                handoff_id,
                "--execute",
                "--repo",
                tmpdir,
                repo_root=repo_root,
                extra_env=env,
            )
            self.assertNotEqual(execute.returncode, 0)
            execute_data = json.loads(execute.stdout)
            self.assertIn("does not pass the resume packet", execute_data["error"])

    def test_cli_inspect_recovers_interrupted_launch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)

            failover = self.run_cli(
                "--json",
                "failover",
                fixture["session_id"],
                "--to-agent",
                "claude",
                "--reason",
                "Recover interrupted launch",
                "--repo",
                tmpdir,
                repo_root=repo_root,
            )
            self.assertEqual(failover.returncode, 0, failover.stderr)
            handoff_id = json.loads(failover.stdout)["handoff_id"]
            launch_id = "la-20260325T182000Z-aaaaaa"

            with SessionTransaction.begin(
                repo_root,
                fixture["session_id"],
                operation="launch.started",
                owner="test:launch:started:cli",
            ) as tx:
                tx.commit(
                    JournalCommitRequest(
                        event_type="launch.started",
                        phase_before="ready_for_handoff",
                        phase_after="launching",
                        payload={"handoff_id": handoff_id, "launch_id": launch_id},
                        timestamp="2026-03-25T18:20:00Z",
                    )
                )

            inspect = self.run_cli(
                "--json",
                "inspect",
                fixture["session_id"],
                "--repo",
                tmpdir,
                repo_root=repo_root,
            )
            self.assertEqual(inspect.returncode, 0, inspect.stderr)
            inspect_data = json.loads(inspect.stdout)

            self.assertEqual(inspect_data["current_status"], "ready_for_handoff")
            self.assertEqual(inspect_data["latest_launch_id"], launch_id)
            self.assertEqual(inspect_data["handoffs"][-1]["launch_status"], "interrupted")


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
