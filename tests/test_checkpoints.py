from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.checkpoints import create_checkpoint
from agent_relay.models import SCHEMA_VERSION, SessionState, ValidationState
from agent_relay.storage import checkpoint_path, load_checkpoint


class CheckpointsTests(TestCase):
    def build_session(self, repo_root: Path) -> SessionState:
        return SessionState(
            schema_version=SCHEMA_VERSION,
            session_id="20260324-120000-abc123",
            repo_root=str(repo_root),
            objective="Relay work between agents",
            workstream_kind="implementation",
            current_agent="codex",
            current_status="active",
            created_at="2026-03-24T12:00:00Z",
            updated_at="2026-03-24T12:02:00Z",
            next_action="Implement checkpoint storage",
            decisions=["Use append-only checkpoint files"],
            blockers=["No summary renderer yet"],
            research_notes=["Need durable snapshots"],
            implementation_notes=["Storage module is next"],
            touched_files=["src/agent_relay/checkpoints.py"],
            validation=ValidationState(status="partial", summary="Unit tests pending"),
            handoffs=[],
            latest_checkpoint_id=None,
        )

    def test_create_checkpoint_generates_id_updates_session_and_mirrors_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self.build_session(repo_root)

            checkpoint = create_checkpoint(repo_root, session, created_at="2026-03-24T12:03:00Z")
            loaded = load_checkpoint(repo_root, session.session_id, checkpoint.checkpoint_id)

            self.assertTrue(checkpoint.checkpoint_id)
            self.assertEqual(session.latest_checkpoint_id, checkpoint.checkpoint_id)
            self.assertTrue(
                checkpoint_path(repo_root, session.session_id, checkpoint.checkpoint_id).exists()
            )
            self.assertEqual(loaded.next_action, session.next_action)
            self.assertEqual(loaded.decisions, session.decisions)
            self.assertEqual(loaded.blockers, session.blockers)
            self.assertEqual(loaded.touched_files, session.touched_files)
            self.assertEqual(loaded.validation.status, session.validation.status)
