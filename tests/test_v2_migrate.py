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

from agent_relay.checkpoints import create_checkpoint
from agent_relay.fs import write_text_atomic
from agent_relay.launcher import build_handoff_record
from agent_relay.models import SCHEMA_VERSION, SessionState, ValidationState
from agent_relay.storage import resume_dir, save_session
from agent_relay.v2.bootstrap import start_session
from agent_relay.v2.migrate import migrate_legacy_session


class AgentRelayV2MigrationTests(TestCase):
    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {"PYTHONPATH": str(ROOT / "src")}
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
        )

    def run_cli_json(self, *args: str) -> dict:
        result = self.run_cli("--json", *args)
        return json.loads(result.stdout)

    def test_legacy_v1_session_is_read_only_until_migrated(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self._create_legacy_session(repo_root)

            result = self.run_cli(
                "--json",
                "checkpoint",
                session.session_id,
                "--next-action",
                "Should be blocked",
                "--repo",
                tmpdir,
            )

            self.assertNotEqual(result.returncode, 0)
            data = json.loads(result.stdout)
            self.assertIn("migrate", data["error"])

    def test_migrate_imports_clean_legacy_session_history_and_preserves_archive(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self._create_legacy_session(repo_root, with_handoff=True, launch_status="succeeded")

            migrate = self.run_cli_json("migrate", session.session_id, "--repo", tmpdir)
            inspect = self.run_cli_json("inspect", session.session_id, "--repo", tmpdir)

            self.assertEqual(migrate["health"], "healthy")
            self.assertTrue((repo_root / ".agent-relay" / "sessions" / session.session_id / "legacy-v1" / "state.json").exists())
            self.assertEqual(inspect["storage_model"], "journal_v2")
            self.assertEqual(inspect["current_agent"], "codex")
            self.assertEqual(inspect["handoffs"][0]["launch_status"], "succeeded")
            self.assertEqual(inspect["handoffs"][0]["to_agent"], "codex")

    def test_migrate_degraded_legacy_session_requires_explicit_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self._create_legacy_session(repo_root, with_handoff=True, launch_status="ready", missing_packet=True)

            migrate = self.run_cli_json("migrate", session.session_id, "--repo", tmpdir)
            inspect_before = self.run_cli_json("inspect", session.session_id, "--repo", tmpdir)

            self.assertEqual(migrate["health"], "degraded")
            self.assertEqual(inspect_before["health"], "degraded")
            self.assertTrue(any("--accept-legacy-import" in item for item in inspect_before["suggested_repair"]))

            repair = self.run_cli_json("repair", session.session_id, "--accept-legacy-import", "--repo", tmpdir)
            self.assertEqual(repair["health_after"], "healthy")

            checkpoint = self.run_cli(
                "--json",
                "checkpoint",
                session.session_id,
                "--next-action",
                "Continue from accepted import",
                "--snapshot-mode",
                "full",
                "--repo",
                tmpdir,
            )
            self.assertEqual(checkpoint.returncode, 0, checkpoint.stderr)

    def test_start_interrupt_rolls_back_partial_v2_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()

            with patch("agent_relay.v2.bootstrap._rebuild_and_validate", side_effect=KeyboardInterrupt("interrupt")):
                with self.assertRaises(KeyboardInterrupt):
                    start_session(
                        repo_root,
                        session_id="start-interrupt",
                        objective="Interrupt bootstrap",
                        workstream_kind="mixed",
                        initial_agent="claude",
                        next_action="Record the next step",
                        snapshot_mode="full",
                        owner="test:start-interrupt",
                    )

            self.assertFalse((repo_root / ".agent-relay" / "sessions" / "start-interrupt").exists())

    def test_migrate_interrupt_restores_legacy_session(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self._create_legacy_session(repo_root)

            with patch("agent_relay.v2.migrate._rebuild_and_validate", side_effect=KeyboardInterrupt("interrupt")):
                with self.assertRaises(KeyboardInterrupt):
                    migrate_legacy_session(repo_root, session.session_id, owner="test:migrate-interrupt")

            legacy_root = repo_root / ".agent-relay" / "sessions" / session.session_id
            self.assertTrue((legacy_root / "state.json").exists())
            self.assertFalse((legacy_root / "session.json").exists())

    def _create_legacy_session(
        self,
        repo_root: Path,
        *,
        with_handoff: bool = False,
        launch_status: str = "ready",
        missing_packet: bool = False,
    ) -> SessionState:
        session = SessionState(
            schema_version=SCHEMA_VERSION,
            session_id="legacy-session",
            repo_root=str(repo_root),
            objective="Import a legacy session",
            workstream_kind="mixed",
            current_agent="claude",
            current_status="active",
            created_at="2026-03-26T10:00:00Z",
            updated_at="2026-03-26T10:00:00Z",
            next_action="Review the imported artifacts",
            decisions=["Keep the raw archive"],
            blockers=[],
            research_notes=[],
            implementation_notes=[],
            touched_files=["src/agent_relay/cli.py"],
            validation=ValidationState(status="partial", summary="Legacy session still needs migration"),
            handoffs=[],
            latest_checkpoint_id=None,
        )
        checkpoint = create_checkpoint(
            repo_root,
            session,
            checkpoint_id="legacy-cp-001",
            created_at="2026-03-26T10:05:00Z",
        )
        save_session(repo_root, session)

        if not with_handoff:
            return session

        packet_path = resume_dir(repo_root, session.session_id) / "codex.md"
        if not missing_packet:
            write_text_atomic(packet_path, "# Codex Resume Packet\n\nContinue the legacy import.\n")
        handoff = build_handoff_record(
            session,
            repo_root=repo_root,
            to_agent="codex",
            reason="Legacy handoff",
            prepared_at="2026-03-26T10:10:00Z",
            resume_path=packet_path,
        )
        handoff.launch_status = launch_status
        handoff.launched_at = "2026-03-26T10:11:00Z"
        handoff.finished_at = "2026-03-26T10:12:00Z"
        handoff.exit_code = 0 if launch_status == "succeeded" else 1
        session.handoffs.append(handoff)

        if launch_status == "ready":
            session.current_status = "handoff_prepared"
        elif launch_status == "succeeded":
            session.current_status = "active"
            session.current_agent = "codex"
        elif launch_status == "failed":
            session.current_status = "launch_failed"
        else:
            session.current_status = "launching"
        session.updated_at = "2026-03-26T10:12:00Z"
        save_session(repo_root, session)
        return session
