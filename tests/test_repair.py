from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.repair import repair_session
from agent_relay.storage import load_session_view
from agent_relay.tx import recover_session_transactions
from tests.session_fixtures import build_sample_session


class AgentRelayRepairTests(TestCase):
    def test_interrupted_repair_rebuild_view_is_recoverable(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            interrupted_path: Path | None = None

            import agent_relay.tx as tx_module

            original_write_json_atomic = tx_module.write_json_atomic

            def interrupt_on_journal_write(path: Path, payload) -> None:
                nonlocal interrupted_path
                if path.parent.name == "journal" and isinstance(payload, dict) and payload.get("kind") == "journal_event":
                    interrupted_path = path
                    raise KeyboardInterrupt("simulated interruption before repair journal commit")
                original_write_json_atomic(path, payload)

            with patch("agent_relay.tx.write_json_atomic", side_effect=interrupt_on_journal_write):
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
