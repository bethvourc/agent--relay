from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.v2.errors import V2ValidationError
from agent_relay.v2.models import (
    CheckpointManifest,
    JournalEvent,
    ManifestFile,
    ObjectRef,
    SCHEMA_VERSION,
    ValidationState,
    object_manifest_from_dict,
)


class V2ModelTests(TestCase):
    def test_journal_event_hash_round_trip(self) -> None:
        event = JournalEvent(
            schema_version=SCHEMA_VERSION,
            kind="journal_event",
            session_id="s1",
            event_id="ev-000001",
            sequence=1,
            type="session.started",
            timestamp="2026-03-25T18:00:00Z",
            tx_id="tx-0001",
            phase_before=None,
            phase_after="active",
            payload={"session_manifest_sha256": "sha256:" + ("1" * 64)},
            object_refs=(),
            prev_event_hash=None,
            event_hash="sha256:" + ("0" * 64),
        )
        hashed = JournalEvent.from_dict({**event.to_dict(), "event_hash": event.expected_event_hash()})

        self.assertEqual(hashed.expected_event_hash(), hashed.event_hash)
        self.assertEqual(JournalEvent.from_dict(hashed.to_dict()), hashed)

    def test_checkpoint_manifest_rejects_missing_summary_file_reference(self) -> None:
        with self.assertRaises(V2ValidationError) as context:
            CheckpointManifest(
                schema_version=SCHEMA_VERSION,
                kind="checkpoint_manifest",
                object_id="cp-1",
                session_id="s1",
                created_at="2026-03-25T18:00:00Z",
                current_agent="claude",
                phase_hint="active",
                task_status="working",
                next_action="Do the next thing",
                decisions=(),
                blockers=(),
                research_notes=(),
                implementation_notes=(),
                touched_files=(),
                validation=ValidationState(status="not_run", summary=""),
                summary_file="summary.md",
                files=(
                    ManifestFile(
                        relative_path="artifacts/repo-state.json",
                        sha256="sha256:" + ("2" * 64),
                        size_bytes=10,
                    ),
                ),
            )

        self.assertIn("summary_file", str(context.exception))

    def test_object_manifest_loader_rejects_unknown_kind(self) -> None:
        with self.assertRaises(V2ValidationError) as context:
            object_manifest_from_dict(
                {
                    "schema_version": SCHEMA_VERSION,
                    "kind": "unknown_manifest",
                }
            )

        self.assertIn("object_manifest.kind", str(context.exception))

    def test_object_ref_requires_relative_manifest_path(self) -> None:
        with self.assertRaises(V2ValidationError) as context:
            ObjectRef(
                object_kind="checkpoint",
                object_id="cp-1",
                manifest_path="/absolute/manifest.json",
                manifest_sha256="sha256:" + ("3" * 64),
            )

        self.assertIn("manifest_path", str(context.exception))
