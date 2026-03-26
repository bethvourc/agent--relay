from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]


class AgentRelayIntegrationFlowTests(TestCase):
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

    def test_bidirectional_demo_flow_preserves_session_history(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            subprocess.run(["git", "init"], cwd=repo_root, text=True, capture_output=True, check=True)
            (repo_root / "src").mkdir()
            (repo_root / "src" / "demo.py").write_text("print('agent relay demo')\n", encoding="utf-8")

            safe_launch_env = {
                "AGENT_RELAY_CODEX_LAUNCH_TEMPLATE": (
                    f"cd {{repo_root}} && {shlex.quote(sys.executable)} -c "
                    "'from pathlib import Path; Path(\"codex-launch.txt\").write_text(\"ok\")' {resume_path}"
                ),
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"cd {{repo_root}} && {shlex.quote(sys.executable)} -c "
                    "'from pathlib import Path; Path(\"claude-launch.txt\").write_text(\"ok\")' {resume_path}"
                ),
            }

            start_data = self.run_cli_json(
                "start",
                "--agent",
                "claude",
                "--task",
                "Validate the full bidirectional handoff flow",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )
            session_id = start_data["session_id"]
            session_root = repo_root / ".agent-relay" / "sessions" / session_id

            self.run_cli_json(
                "checkpoint",
                session_id,
                "--next-action",
                "Prepare the first Codex handoff",
                "--decision",
                "Use a safe launch override for the integration test",
                "--capture-git-changes",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )

            self.run_cli_json(
                "prepare",
                session_id,
                "--next-action",
                "Hand off to Codex and continue implementation",
                "--validation-status",
                "partial",
                "--validation-summary",
                "Launch path still needs an end-to-end check",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )

            first_failover = self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "integration walkthrough step one",
                "--resume-evidence-depth",
                "full",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )
            first_launch = self.run_cli(
                "--json",
                "launch",
                session_id,
                "--repo",
                tmpdir,
                "--execute",
                extra_env=safe_launch_env,
            )
            self.assertEqual(first_launch.returncode, 0)
            self.assertTrue((repo_root / "codex-launch.txt").exists())
            self.assertTrue((session_root / "resume" / "codex.md").exists())
            self.assertIn("# Codex Resume Packet", (session_root / "resume" / "codex.md").read_text())

            self.run_cli_json(
                "checkpoint",
                session_id,
                "--next-action",
                "Prepare a return handoff to Claude",
                "--implementation-note",
                "Codex completed the implementation slice and wants review",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )

            self.run_cli_json(
                "prepare",
                session_id,
                "--next-action",
                "Return to Claude for validation and close-out",
                "--validation-status",
                "partial",
                "--validation-summary",
                "Implementation is complete but final review is still pending",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )

            second_failover = self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "claude",
                "--reason",
                "integration walkthrough return step",
                "--resume-evidence-depth",
                "full",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )
            second_launch = self.run_cli(
                "--json",
                "launch",
                session_id,
                "--repo",
                tmpdir,
                "--execute",
                extra_env=safe_launch_env,
            )
            self.assertEqual(second_launch.returncode, 0)
            self.assertTrue((repo_root / "claude-launch.txt").exists())
            self.assertTrue((session_root / "resume" / "claude.md").exists())
            self.assertIn("# Claude Code Resume Packet", (session_root / "resume" / "claude.md").read_text())

            final_checkpoint = self.run_cli_json(
                "checkpoint",
                session_id,
                "--next-action",
                "Ship the validated demo flow",
                "--decision",
                "The same session can survive multiple handoffs",
                "--validation-status",
                "passed",
                "--validation-summary",
                "Bidirectional handoff demo completed successfully",
                "--repo",
                tmpdir,
                extra_env=safe_launch_env,
            )
            state = self.run_cli_json("inspect", session_id, "--repo", tmpdir, extra_env=safe_launch_env)
            summary = (session_root / "summary.md").read_text()

            self.assertEqual(first_failover["from_agent"], "claude")
            self.assertEqual(first_failover["to_agent"], "codex")
            self.assertEqual(second_failover["from_agent"], "codex")
            self.assertEqual(second_failover["to_agent"], "claude")
            self.assertEqual(state["current_agent"], "claude")
            self.assertEqual(state["current_status"], "active")
            self.assertEqual(state["validation"]["status"], "passed")
            self.assertEqual(state["latest_checkpoint_id"], final_checkpoint["checkpoint_id"])
            self.assertEqual(len(state["handoffs"]), 2)
            self.assertEqual(state["handoffs"][0]["launch_status"], "succeeded")
            self.assertEqual(state["handoffs"][1]["launch_status"], "succeeded")
            self.assertNotEqual(state["handoffs"][0]["checkpoint_id"], state["handoffs"][1]["checkpoint_id"])
            self.assertIn("Ship the validated demo flow", summary)
            self.assertIn("Bidirectional handoff demo completed successfully", summary)
