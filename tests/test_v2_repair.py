from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.v2.repair import repair_session
from agent_relay.v2.storage import load_session_view
from agent_relay.v2.tx import recover_session_transactions
from tests.v2_fixtures import build_sample_v2_session


class AgentRelayV2RepairTests(TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT / "src")}
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def test_inspect_surfaces_degraded_session_with_last_valid_event_and_repair_suggestion(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_v2_session(repo_root)
            packet_path = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "objects"
                / "handoffs"
                / fixture["handoff_id"]
                / "packet.md"
            )
            packet_path.write_text("# tampered\n", encoding="utf-8")

            result = self.run_cli("--json", "inspect", fixture["session_id"], "--repo", tmpdir)

            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)
            self.assertEqual(data["health"], "degraded")
            self.assertEqual(data["last_valid_event"]["event_id"], "ev-000002")
            self.assertIn(str(packet_path), data["broken_paths"])
            self.assertTrue(any("--promote-last-good" in item for item in data["suggested_repair"]))

    def test_mutating_v2_command_is_blocked_while_repair_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_v2_session(repo_root)
            packet_path = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "objects"
                / "handoffs"
                / fixture["handoff_id"]
                / "packet.md"
            )
            packet_path.write_text("# tampered\n", encoding="utf-8")

            result = self.run_cli(
                "--json",
                "checkpoint",
                fixture["session_id"],
                "--next-action",
                "Should be blocked",
                "--snapshot-mode",
                "full",
                "--repo",
                tmpdir,
            )

            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertIn("blocked while session health is degraded", data["error"])
            self.assertIn("--promote-last-good", data["error"])

    def test_repair_promote_last_good_quarantines_tail_and_restores_healthy_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_v2_session(repo_root)
            packet_path = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "objects"
                / "handoffs"
                / fixture["handoff_id"]
                / "packet.md"
            )
            packet_path.write_text("# tampered\n", encoding="utf-8")

            repair = self.run_cli(
                "--json",
                "repair",
                fixture["session_id"],
                "--promote-last-good",
                "--repo",
                tmpdir,
            )
            self.assertEqual(repair.returncode, 0, repair.stderr)
            repair_data = json.loads(repair.stdout)

            inspect = self.run_cli("--json", "inspect", fixture["session_id"], "--repo", tmpdir)
            self.assertEqual(inspect.returncode, 0, inspect.stderr)
            inspect_data = json.loads(inspect.stdout)

            self.assertEqual(repair_data["health_before"], "degraded")
            self.assertEqual(repair_data["health_after"], "healthy")
            self.assertTrue(Path(repair_data["repair_log_path"]).exists())
            self.assertTrue(repair_data["repair_event_id"])
            self.assertTrue(repair_data["quarantined_paths"])
            self.assertEqual(inspect_data["health"], "healthy")
            self.assertEqual(inspect_data["current_status"], "ready_for_handoff")
            self.assertEqual(inspect_data["latest_checkpoint_id"], fixture["checkpoint_one_id"])
            self.assertEqual(inspect_data["prepared_handoff_id"], None)

    def test_repair_rollback_pending_quarantines_residue_and_restores_health(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_v2_session(repo_root)
            pending_root = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "recovery"
                / "pending-tx"
                / "tx-bad"
            )
            pending_root.mkdir(parents=True, exist_ok=True)

            before = self.run_cli("--json", "inspect", fixture["session_id"], "--repo", tmpdir)
            before_data = json.loads(before.stdout)
            self.assertEqual(before_data["health"], "degraded")
            self.assertTrue(any("--rollback-pending" in item for item in before_data["suggested_repair"]))

            repair = self.run_cli(
                "--json",
                "repair",
                fixture["session_id"],
                "--rollback-pending",
                "--repo",
                tmpdir,
            )
            self.assertEqual(repair.returncode, 0, repair.stderr)
            repair_data = json.loads(repair.stdout)

            inspect = self.run_cli("--json", "inspect", fixture["session_id"], "--repo", tmpdir)
            inspect_data = json.loads(inspect.stdout)
            quarantine_root = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "recovery"
                / "quarantine"
            )

            self.assertEqual(repair_data["health_after"], "healthy")
            self.assertEqual(repair_data["cleaned_pending_transactions"], 1)
            self.assertEqual(inspect_data["health"], "healthy")
            self.assertFalse(pending_root.exists())
            self.assertTrue(any(path.name.startswith("tx-bad-broken") for path in quarantine_root.iterdir()))

    def test_repair_rebuild_view_restores_missing_caches_for_healthy_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_v2_session(repo_root)
            view_path = repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "derived" / "view.json"
            head_path = repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "refs" / "head.json"
            view_path.unlink(missing_ok=True)
            head_path.unlink(missing_ok=True)

            repair = self.run_cli(
                "--json",
                "repair",
                fixture["session_id"],
                "--rebuild-view",
                "--repo",
                tmpdir,
            )
            self.assertEqual(repair.returncode, 0, repair.stderr)
            repair_data = json.loads(repair.stdout)

            self.assertEqual(repair_data["health_before"], "healthy")
            self.assertEqual(repair_data["health_after"], "healthy")
            self.assertTrue(repair_data["repair_event_id"])
            self.assertTrue(view_path.exists())
            self.assertTrue(head_path.exists())

    def test_interrupted_repair_rebuild_view_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_v2_session(repo_root)
            interrupted_path: Path | None = None

            import agent_relay.v2.tx as tx_module

            original_write_json_atomic = tx_module.write_json_atomic

            def interrupt_on_journal_write(path: Path, payload) -> None:
                nonlocal interrupted_path
                if path.parent.name == "journal" and isinstance(payload, dict) and payload.get("kind") == "journal_event":
                    interrupted_path = path
                    raise KeyboardInterrupt("simulated interruption before repair journal commit")
                original_write_json_atomic(path, payload)

            with patch("agent_relay.v2.tx.write_json_atomic", side_effect=interrupt_on_journal_write):
                with self.assertRaises(KeyboardInterrupt):
                    repair_session(
                        repo_root,
                        fixture["session_id"],
                        action="rebuild_view",
                        owner="test:repair:interrupt",
                    )

            view = load_session_view(repo_root, fixture["session_id"])
            pending_root = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "recovery"
                / "pending-tx"
            )

            self.assertIsNotNone(interrupted_path)
            self.assertFalse(interrupted_path.exists())
            self.assertEqual(view.phase, "active")
            self.assertTrue(any(path.is_dir() for path in pending_root.iterdir()))

            report = recover_session_transactions(repo_root, fixture["session_id"])
            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(report.quarantined_transactions, 1)
            self.assertEqual(view.phase, "active")
