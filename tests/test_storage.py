from __future__ import annotations

import tempfile
import sys
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.models import CheckpointRecord, SCHEMA_VERSION, SessionState, ValidationState
from agent_relay.storage import (
    checkpoints_dir,
    load_checkpoint,
    load_session,
    save_checkpoint,
    save_session,
    summary_path,
)


class StorageTests(TestCase):
    def build_session(self, repo_root: Path) -> SessionState:
        return SessionState(
            schema_version=SCHEMA_VERSION,
            session_id="20260324-120000-abc123",
            repo_root=str(repo_root),
            objective="Relay work between agents",
            workstream_kind="mixed",
            current_agent="claude",
            current_status="active",
            created_at="2026-03-24T12:00:00Z",
            updated_at="2026-03-24T12:00:00Z",
            next_action="Write the first checkpoint",
            decisions=[],
            blockers=[],
            research_notes=[],
            implementation_notes=[],
            touched_files=[],
            validation=ValidationState(status="not_run", summary=""),
            handoffs=[],
            latest_checkpoint_id=None,
        )

    def test_save_and_load_session_preserves_content_and_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self.build_session(repo_root)

            save_session(repo_root, session)
            loaded = load_session(repo_root, session.session_id)

            self.assertEqual(loaded, session)
            self.assertTrue(checkpoints_dir(repo_root, session.session_id).exists())
            self.assertTrue(summary_path(repo_root, session.session_id).parent.exists())

    def test_save_checkpoint_writes_expected_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self.build_session(repo_root)
            checkpoint = CheckpointRecord(
                checkpoint_id="20260324-120100-def456",
                session_id=session.session_id,
                created_at="2026-03-24T12:01:00Z",
                status=session.current_status,
                next_action=session.next_action,
                decisions=[],
                blockers=[],
                research_notes=[],
                implementation_notes=[],
                touched_files=[],
                validation=session.validation,
                artifacts={},
            )

            path = save_checkpoint(repo_root, checkpoint)
            loaded = load_checkpoint(repo_root, session.session_id, checkpoint.checkpoint_id)

            self.assertEqual(path, checkpoints_dir(repo_root, session.session_id) / f"{checkpoint.checkpoint_id}.json")
            self.assertEqual(loaded, checkpoint)
