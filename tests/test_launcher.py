from __future__ import annotations

import shlex
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.launcher import (  # noqa: E402
    build_handoff_record,
    launch_handoff,
    launch_preview_lines,
)
from agent_relay.models import CheckpointRecord, SCHEMA_VERSION, SessionState, ValidationState  # noqa: E402
from agent_relay.storage import save_checkpoint, save_session  # noqa: E402


class LauncherTests(TestCase):
    def build_session(self, repo_root: Path) -> SessionState:
        return SessionState(
            schema_version=SCHEMA_VERSION,
            session_id="20260324-120000-abc123",
            repo_root=str(repo_root),
            objective="Launch the next agent",
            workstream_kind="mixed",
            current_agent="claude",
            current_status="handoff_prepared",
            created_at="2026-03-24T12:00:00Z",
            updated_at="2026-03-24T12:05:00Z",
            next_action="Run the prepared launch command",
            decisions=["Keep state local-first"],
            blockers=[],
            research_notes=[],
            implementation_notes=[],
            touched_files=["src/agent_relay/launcher.py"],
            validation=ValidationState(status="not_run", summary=""),
            handoffs=[],
            latest_checkpoint_id="20260324-120400-def456",
        )

    def build_checkpoint(self, session: SessionState) -> CheckpointRecord:
        return CheckpointRecord(
            checkpoint_id=session.latest_checkpoint_id or "20260324-120400-def456",
            session_id=session.session_id,
            created_at="2026-03-24T12:04:00Z",
            status="active",
            next_action=session.next_action,
            decisions=list(session.decisions),
            blockers=list(session.blockers),
            research_notes=list(session.research_notes),
            implementation_notes=list(session.implementation_notes),
            touched_files=list(session.touched_files),
            validation=session.validation,
            artifacts={},
        )

    def test_build_handoff_record_uses_latest_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self.build_session(repo_root)
            handoff = build_handoff_record(
                session,
                repo_root=repo_root,
                to_agent="codex",
                reason="manual switch",
                prepared_at="2026-03-24T12:05:00Z",
                resume_path=repo_root / ".agent-relay" / "sessions" / session.session_id / "resume" / "codex.md",
            )

            self.assertEqual(handoff.checkpoint_id, session.latest_checkpoint_id)
            self.assertEqual(handoff.launch_status, "ready")
            self.assertEqual(handoff.launch_profile, "Codex")

    def test_launch_preview_lines_show_target_resume_and_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self.build_session(repo_root)
            handoff = build_handoff_record(
                session,
                repo_root=repo_root,
                to_agent="codex",
                reason="manual switch",
                prepared_at="2026-03-24T12:05:00Z",
                resume_path=repo_root / ".agent-relay" / "sessions" / session.session_id / "resume" / "codex.md",
            )

            preview = launch_preview_lines(handoff)

            self.assertEqual(preview[0], "Launch target: codex")
            self.assertEqual(preview[1], handoff.resume_packet_path)
            self.assertEqual(preview[2], handoff.launch_command)
            self.assertEqual(preview[3], handoff.launch_instructions)

    def test_launch_handoff_success_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self.build_session(repo_root)
            checkpoint = self.build_checkpoint(session)
            save_checkpoint(repo_root, checkpoint)
            save_session(repo_root, session)

            launch_template = (
                f"cd {{repo_root}} && {shlex.quote(sys.executable)} -c "
                "'from pathlib import Path; Path(\"launch-success.txt\").write_text(\"ok\")'"
            )
            with patch.dict("os.environ", {"AGENT_RELAY_CODEX_LAUNCH_TEMPLATE": launch_template}, clear=False):
                handoff = build_handoff_record(
                    session,
                    repo_root=repo_root,
                    to_agent="codex",
                    reason="manual switch",
                    prepared_at="2026-03-24T12:05:00Z",
                    resume_path=repo_root / ".agent-relay" / "sessions" / session.session_id / "resume" / "codex.md",
                )

            session.handoffs.append(handoff)
            exit_code = launch_handoff(repo_root, session, handoff)

            self.assertEqual(exit_code, 0)
            self.assertEqual(handoff.launch_status, "succeeded")
            self.assertEqual(session.current_agent, "codex")
            self.assertEqual(session.current_status, "active")
            self.assertTrue((repo_root / "launch-success.txt").exists())

    def test_launch_handoff_failure_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            session = self.build_session(repo_root)
            checkpoint = self.build_checkpoint(session)
            save_checkpoint(repo_root, checkpoint)
            save_session(repo_root, session)

            launch_template = f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(7)'"
            with patch.dict("os.environ", {"AGENT_RELAY_CODEX_LAUNCH_TEMPLATE": launch_template}, clear=False):
                handoff = build_handoff_record(
                    session,
                    repo_root=repo_root,
                    to_agent="codex",
                    reason="manual switch",
                    prepared_at="2026-03-24T12:05:00Z",
                    resume_path=repo_root / ".agent-relay" / "sessions" / session.session_id / "resume" / "codex.md",
                )

            session.handoffs.append(handoff)
            exit_code = launch_handoff(repo_root, session, handoff)

            self.assertEqual(exit_code, 7)
            self.assertEqual(handoff.launch_status, "failed")
            self.assertEqual(handoff.exit_code, 7)
            self.assertEqual(session.current_agent, "claude")
            self.assertEqual(session.current_status, "launch_failed")
