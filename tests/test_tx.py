from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.layout import derived_view_path, head_ref_path, pending_tx_dir
from agent_relay.storage import load_session_view
from agent_relay.tx import JournalCommitRequest, SessionTransaction, recover_session_transactions
from tests.session_fixtures import build_checkpoint_object, build_sample_session


class TransactionTests(TestCase):
    def test_commit_promotes_objects_writes_journal_and_rebuilds_stale_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            view_path = derived_view_path(repo_root, fixture["session_id"])
            head_path = head_ref_path(repo_root, fixture["session_id"])
            view_path.parent.mkdir(parents=True, exist_ok=True)
            head_path.parent.mkdir(parents=True, exist_ok=True)
            view_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "kind": "derived_session_view",
                        "session_id": fixture["session_id"],
                        "storage_model": "journal_v2",
                        "repo_root": str(repo_root),
                        "objective": "stale",
                        "workstream_kind": "mixed",
                        "created_at": "2026-03-25T18:00:00Z",
                        "updated_at": "2026-03-25T18:00:00Z",
                        "initial_agent": "claude",
                        "current_agent": "claude",
                        "phase": "active",
                        "current_status": "active",
                        "task_status": None,
                        "next_action": "",
                        "decisions": [],
                        "blockers": [],
                        "research_notes": [],
                        "implementation_notes": [],
                        "touched_files": [],
                        "validation": {"status": "not_run", "summary": ""},
                        "latest_checkpoint_id": None,
                        "prepared_handoff_id": None,
                        "latest_launch_id": None,
                        "last_resume_handoff_id": None,
                        "event_count": 1,
                        "last_event_id": "ev-000001",
                        "last_event_hash": "sha256:" + ("0" * 64),
                        "built_from_sequence": 1,
                        "built_from_event_hash": "sha256:" + ("0" * 64),
                        "health": "healthy",
                        "handoffs": [],
                        "checkpoint_ids": [],
                        "launch_ids": [],
                        "alerts": [],
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            head_path.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "kind": "session_head_ref",
                        "session_id": fixture["session_id"],
                        "last_event_id": "ev-000001",
                        "last_sequence": 1,
                        "last_event_hash": "sha256:" + ("0" * 64),
                        "updated_at": "2026-03-25T18:00:00Z",
                    },
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            checkpoint_id = "cp-20260325T181000Z-555555"
            manifest, file_contents = build_checkpoint_object(
                session_id=fixture["session_id"],
                object_id=checkpoint_id,
                created_at="2026-03-25T18:10:00Z",
                current_agent="codex",
                next_action="Commit the Phase 2 transaction engine",
            )

            with SessionTransaction.begin(
                repo_root,
                fixture["session_id"],
                operation="checkpoint.recorded",
                owner="test-commit",
            ) as tx:
                tx.stage_manifest_object(manifest, file_contents=file_contents)
                event = tx.commit(
                    JournalCommitRequest(
                        event_type="checkpoint.recorded",
                        phase_before="active",
                        phase_after="active",
                        payload={"checkpoint_id": checkpoint_id},
                        timestamp="2026-03-25T18:10:00Z",
                    )
                )
                tx_id = tx.tx_id

            view = load_session_view(repo_root, fixture["session_id"])
            updated_cache = json.loads(view_path.read_text(encoding="utf-8"))
            updated_head = json.loads(head_path.read_text(encoding="utf-8"))
            pending_root = pending_tx_dir(repo_root, fixture["session_id"])

            self.assertEqual(event.sequence, 8)
            self.assertEqual(view.latest_checkpoint_id, checkpoint_id)
            self.assertEqual(updated_cache["latest_checkpoint_id"], checkpoint_id)
            self.assertEqual(updated_head["last_sequence"], 8)
            self.assertFalse((pending_root / tx_id).exists())

    def test_recovery_quarantines_promoted_uncommitted_transactions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            checkpoint_id = "cp-20260325T181100Z-666666"
            manifest, file_contents = build_checkpoint_object(
                session_id=fixture["session_id"],
                object_id=checkpoint_id,
                created_at="2026-03-25T18:11:00Z",
                current_agent="codex",
                next_action="Simulate a crash before journal commit",
            )

            with SessionTransaction.begin(
                repo_root,
                fixture["session_id"],
                operation="checkpoint.recorded",
                owner="test-promote-only",
            ) as tx:
                tx.stage_manifest_object(manifest, file_contents=file_contents)
                tx._promote_staged_objects()
                tx_id = tx.tx_id

            report = recover_session_transactions(repo_root, fixture["session_id"])
            view = load_session_view(repo_root, fixture["session_id"])
            quarantine = repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "recovery" / "quarantine"
            promoted_dir = (
                repo_root / ".agent-relay" / "sessions" / fixture["session_id"] / "objects" / "checkpoints" / checkpoint_id
            )

            self.assertEqual(report.quarantined_transactions, 1)
            self.assertFalse(promoted_dir.exists())
            self.assertTrue(any(path.name.startswith(tx_id) for path in quarantine.iterdir()))
            self.assertEqual(view.latest_checkpoint_id, fixture["checkpoint_two_id"])

    def test_recovery_rebuilds_caches_for_committed_pending_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            checkpoint_id = "cp-20260325T181200Z-777777"
            manifest, file_contents = build_checkpoint_object(
                session_id=fixture["session_id"],
                object_id=checkpoint_id,
                created_at="2026-03-25T18:12:00Z",
                current_agent="codex",
                next_action="Recover after the journal write",
            )

            with SessionTransaction.begin(
                repo_root,
                fixture["session_id"],
                operation="checkpoint.recorded",
                owner="test-committed-pending",
            ) as tx:
                tx.stage_manifest_object(manifest, file_contents=file_contents)
                tx.commit(
                    JournalCommitRequest(
                        event_type="checkpoint.recorded",
                        phase_before="active",
                        phase_after="active",
                        payload={"checkpoint_id": checkpoint_id},
                        timestamp="2026-03-25T18:12:00Z",
                    ),
                    cleanup=False,
                )
                tx_id = tx.tx_id

            derived_view_path(repo_root, fixture["session_id"]).unlink()
            head_ref_path(repo_root, fixture["session_id"]).unlink()

            report = recover_session_transactions(repo_root, fixture["session_id"])
            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(report.cleaned_committed_transactions, 1)
            self.assertTrue(report.rebuilt_caches)
            self.assertEqual(view.latest_checkpoint_id, checkpoint_id)
            self.assertFalse((pending_tx_dir(repo_root, fixture["session_id"]) / tx_id).exists())
