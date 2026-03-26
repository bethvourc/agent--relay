from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.models import (
    ModelValidationError,
    SCHEMA_VERSION,
    HandoffRecord,
    SessionState,
    ValidationState,
)


class ModelsTests(TestCase):
    def test_session_round_trip_serialization(self) -> None:
        session = SessionState(
            schema_version=SCHEMA_VERSION,
            session_id="20260324-120000-abc123",
            repo_root="/tmp/project",
            objective="Relay work between agents",
            workstream_kind="mixed",
            current_agent="claude",
            current_status="handoff_prepared",
            created_at="2026-03-24T12:00:00Z",
            updated_at="2026-03-24T12:05:00Z",
            next_action="Open the resume packet",
            decisions=["Keep state local-first"],
            blockers=["Waiting for launch"],
            research_notes=["Need a clean handoff flow"],
            implementation_notes=["Launch command is templated"],
            touched_files=["src/agent_relay/cli.py"],
            validation=ValidationState(status="partial", summary="Manual checks only"),
            handoffs=[
                HandoffRecord(
                    from_agent="claude",
                    to_agent="codex",
                    reason="rate limit reached",
                    prepared_at="2026-03-24T12:04:00Z",
                    checkpoint_id="20260324-120400-def456",
                    resume_packet_path="/tmp/project/.agent-relay/sessions/s1/resume/codex.md",
                    launch_status="ready",
                    launch_profile="Codex",
                    launch_cwd="/tmp/project",
                    launch_command="cd /tmp/project && codex --resume /tmp/project/.agent-relay/sessions/s1/resume/codex.md",
                    launch_template="cd {repo_root} && {agent_cli} --resume {resume_path}",
                    launch_template_source="default",
                    launch_instructions="Start Codex in /tmp/project with /tmp/project/.agent-relay/sessions/s1/resume/codex.md as the resume packet input.",
                )
            ],
            latest_checkpoint_id="20260324-120400-def456",
        )

        loaded = SessionState.from_dict(session.to_dict())

        self.assertEqual(loaded, session)

    def test_invalid_validation_status_fails_clearly(self) -> None:
        with self.assertRaises(ModelValidationError) as context:
            ValidationState(status="unknown", summary="")

        self.assertIn("validation.status", str(context.exception))

    def test_missing_required_session_field_fails_clearly(self) -> None:
        with self.assertRaises(ModelValidationError) as context:
            SessionState.from_dict(
                {
                    "schema_version": SCHEMA_VERSION,
                    "session_id": "s1",
                    "repo_root": "/tmp/project",
                    "workstream_kind": "mixed",
                    "current_agent": "claude",
                    "current_status": "active",
                    "created_at": "2026-03-24T12:00:00Z",
                    "updated_at": "2026-03-24T12:00:00Z",
                    "next_action": "",
                    "decisions": [],
                    "blockers": [],
                    "research_notes": [],
                    "implementation_notes": [],
                    "touched_files": [],
                    "validation": {"status": "not_run", "summary": ""},
                    "handoffs": [],
                }
            )

        self.assertIn("objective", str(context.exception))
