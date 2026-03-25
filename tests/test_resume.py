from __future__ import annotations

import sys
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.models import (  # noqa: E402
    CheckpointRecord,
    HandoffRecord,
    SCHEMA_VERSION,
    SessionState,
    ValidationState,
)
from agent_relay.resume import ResumeRenderOptions, render_resume_packet  # noqa: E402


class ResumeTests(TestCase):
    def build_session(self) -> SessionState:
        return SessionState(
            schema_version=SCHEMA_VERSION,
            session_id="20260324-120000-abc123",
            repo_root="/tmp/project",
            objective="Move work between Claude Code and Codex",
            workstream_kind="mixed",
            current_agent="claude",
            current_status="handoff_prepared",
            created_at="2026-03-24T12:00:00Z",
            updated_at="2026-03-24T12:05:00Z",
            next_action="Render the Codex resume packet",
            decisions=["Keep state local-first"],
            blockers=["Need a reliable launch path"],
            research_notes=["Compare handoff structure across agents"],
            implementation_notes=["Launch metadata is already recorded"],
            touched_files=["src/agent_relay/cli.py", "src/agent_relay/resume.py"],
            validation=ValidationState(status="partial", summary="Unit tests only"),
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
                    launch_command="cd /tmp/project && codex",
                    launch_template="cd {repo_root} && {agent_cli}",
                    launch_template_source="default",
                    launch_instructions="Start Codex in /tmp/project",
                )
            ],
            latest_checkpoint_id="20260324-120400-def456",
        )

    def build_checkpoint(self) -> CheckpointRecord:
        return CheckpointRecord(
            checkpoint_id="20260324-120400-def456",
            session_id="20260324-120000-abc123",
            created_at="2026-03-24T12:04:00Z",
            status="active",
            next_action="Render the Codex resume packet",
            decisions=["Keep state local-first"],
            blockers=["Need a reliable launch path"],
            research_notes=["Compare handoff structure across agents"],
            implementation_notes=["Launch metadata is already recorded"],
            touched_files=["src/agent_relay/cli.py", "src/agent_relay/resume.py"],
            validation=ValidationState(status="partial", summary="Unit tests only"),
            artifacts={
                "commands": ["python3 -m unittest discover -s tests", "python3 -m agent_relay.cli --help"],
                "patch": "artifacts/patches/resume.patch",
            },
        )

    def test_codex_resume_includes_checkpoint_snapshot_and_context(self) -> None:
        session = self.build_session()
        checkpoint = self.build_checkpoint()

        packet = render_resume_packet(
            session,
            checkpoint,
            "codex",
            handoff_reason="manual switch",
            prepared_at="2026-03-24T12:05:00Z",
        )

        self.assertIn("# Codex Resume Packet", packet)
        self.assertIn("Latest checkpoint:", packet)
        self.assertIn("Checkpoint id: 20260324-120400-def456", packet)
        self.assertIn("Decisions to preserve:", packet)
        self.assertIn("Recent handoffs:", packet)
        self.assertIn("Latest checkpoint artifacts:", packet)
        self.assertIn("- commands: 2 item(s)", packet)

    def test_claude_resume_uses_full_evidence_depth(self) -> None:
        session = self.build_session()
        checkpoint = self.build_checkpoint()

        packet = render_resume_packet(
            session,
            checkpoint,
            "claude",
            handoff_reason="manual switch",
            prepared_at="2026-03-24T12:05:00Z",
            options=ResumeRenderOptions(evidence_depth="full"),
        )

        self.assertIn("# Claude Code Resume Packet", packet)
        self.assertIn("Latest checkpoint artifacts:", packet)
        self.assertIn("- commands:", packet)
        self.assertIn("python3 -m unittest discover -s tests", packet)
        self.assertIn("artifacts/patches/resume.patch", packet)

    def test_minimal_evidence_depth_omits_artifact_section(self) -> None:
        session = self.build_session()
        checkpoint = self.build_checkpoint()

        packet = render_resume_packet(
            session,
            checkpoint,
            "codex",
            handoff_reason="manual switch",
            prepared_at="2026-03-24T12:05:00Z",
            options=ResumeRenderOptions(evidence_depth="minimal"),
        )

        self.assertNotIn("Latest checkpoint artifacts:", packet)
