from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import TestCase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.errors import CorruptionError
from agent_relay.layout import derived_view_path, head_ref_path
from agent_relay.models import JournalEvent
from agent_relay.storage import load_session_view
from tests.session_fixtures import build_sample_session


class ReplayTests(TestCase):
    def test_load_session_view_rebuilds_from_journal_and_objects(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)

            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(view.storage_model, "journal_v2")
            self.assertEqual(view.current_agent, "codex")
            self.assertEqual(view.current_status, "active")
            self.assertEqual(view.latest_checkpoint_id, fixture["checkpoint_two_id"])
            self.assertEqual(view.prepared_handoff_id, None)
            self.assertEqual(view.last_resume_handoff_id, fixture["handoff_id"])
            self.assertEqual(view.handoffs[0].launch_status, "succeeded")
            self.assertTrue(derived_view_path(repo_root, fixture["session_id"]).exists())
            self.assertTrue(head_ref_path(repo_root, fixture["session_id"]).exists())

    def test_load_session_view_rebuilds_stale_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            stale_view_path = derived_view_path(repo_root, fixture["session_id"])
            stale_head_path = head_ref_path(repo_root, fixture["session_id"])
            stale_view_path.parent.mkdir(parents=True, exist_ok=True)
            stale_head_path.parent.mkdir(parents=True, exist_ok=True)
            stale_view_path.write_text(
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
            stale_head_path.write_text(
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

            view = load_session_view(repo_root, fixture["session_id"])
            rewritten = json.loads(stale_view_path.read_text(encoding="utf-8"))

            self.assertEqual(view.latest_checkpoint_id, fixture["checkpoint_two_id"])
            self.assertEqual(rewritten["latest_checkpoint_id"], fixture["checkpoint_two_id"])
            self.assertEqual(rewritten["current_agent"], "codex")

    def test_load_session_view_rejects_hash_chain_break(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            event_path = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "journal"
                / "000003-handoff.prepared.json"
            )
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["prev_event_hash"] = "sha256:" + ("9" * 64)
            event_path.write_text(json.dumps(event, indent=2) + "\n", encoding="utf-8")

            with self.assertRaises(CorruptionError) as context:
                load_session_view(repo_root, fixture["session_id"])

            self.assertIn("hash chain broken", str(context.exception))

    def test_load_session_view_rejects_object_hash_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
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
            packet_path.write_text("# mutated\n", encoding="utf-8")

            with self.assertRaises(CorruptionError) as context:
                load_session_view(repo_root, fixture["session_id"])

            self.assertIn("object file hash mismatch", str(context.exception))

    def test_load_session_view_rejects_checkpoint_transition_outside_state_machine(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = build_sample_session(repo_root)
            event_path = (
                repo_root
                / ".agent-relay"
                / "sessions"
                / fixture["session_id"]
                / "journal"
                / "000007-checkpoint.recorded.json"
            )
            event = json.loads(event_path.read_text(encoding="utf-8"))
            event["phase_after"] = "paused"
            updated = JournalEvent.from_dict({**event, "event_hash": "sha256:" + ("0" * 64)})
            event["event_hash"] = updated.expected_event_hash()
            event_path.write_text(json.dumps(event, indent=2) + "\n", encoding="utf-8")

            with self.assertRaises(CorruptionError) as context:
                load_session_view(repo_root, fixture["session_id"])

            self.assertIn("lifecycle state machine", str(context.exception))
