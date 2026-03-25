from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase, skipUnless
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.capture import (  # noqa: E402
    AUTOSAVE_RESEARCH_NOTE_FILE_ENV,
    AUTOSAVE_VALIDATION_SUMMARY_FILE_ENV,
    CaptureOptions,
    apply_capture_options,
    capture_git_touched_files,
    capture_session,
)
from agent_relay.models import SCHEMA_VERSION, SessionState, ValidationState  # noqa: E402


class CaptureTests(TestCase):
    def build_session(self, repo_root: Path) -> SessionState:
        return SessionState(
            schema_version=SCHEMA_VERSION,
            session_id="20260324-120000-abc123",
            repo_root=str(repo_root),
            objective="Capture the latest working state",
            workstream_kind="mixed",
            current_agent="claude",
            current_status="active",
            created_at="2026-03-24T12:00:00Z",
            updated_at="2026-03-24T12:00:00Z",
            next_action="Record the checkpoint",
            decisions=[],
            blockers=[],
            research_notes=[],
            implementation_notes=[],
            touched_files=[],
            validation=ValidationState(status="not_run", summary=""),
            handoffs=[],
            latest_checkpoint_id=None,
        )

    def test_capture_session_reads_note_files_and_validation_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            notes_dir = repo_root / "notes"
            notes_dir.mkdir()
            (notes_dir / "research.md").write_text("Investigated the handoff edge cases\n", encoding="utf-8")
            (notes_dir / "implementation.md").write_text("Refactored the capture helper\n", encoding="utf-8")
            (notes_dir / "validation.txt").write_text("Smoke tests still need to run\n", encoding="utf-8")

            session = self.build_session(repo_root)
            checkpoint = capture_session(
                repo_root,
                session,
                options=CaptureOptions(
                    status="paused",
                    next_action="Hand off to Codex",
                    decisions=["Keep auto-capture optional"],
                    research_note_file="notes/research.md",
                    implementation_note_file="notes/implementation.md",
                    validation_status="partial",
                    validation_summary_file="notes/validation.txt",
                ),
            )

            self.assertEqual(session.current_status, "paused")
            self.assertEqual(session.next_action, "Hand off to Codex")
            self.assertEqual(session.validation.status, "partial")
            self.assertEqual(session.validation.summary, "Smoke tests still need to run")
            self.assertIn("Investigated the handoff edge cases", session.research_notes)
            self.assertIn("Refactored the capture helper", session.implementation_notes)
            self.assertEqual(
                checkpoint.artifacts["research_note_source"],
                str((notes_dir / "research.md").resolve()),
            )
            self.assertEqual(
                checkpoint.artifacts["implementation_note_source"],
                str((notes_dir / "implementation.md").resolve()),
            )
            self.assertEqual(
                checkpoint.artifacts["validation_summary_source"],
                str((notes_dir / "validation.txt").resolve()),
            )

    def test_apply_capture_options_uses_env_defaults_without_duplicate_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            notes_dir = repo_root / "notes"
            notes_dir.mkdir()
            (notes_dir / "research.md").write_text("Same note every time\n", encoding="utf-8")
            (notes_dir / "validation.txt").write_text("Validation is pending\n", encoding="utf-8")

            session = self.build_session(repo_root)
            session.research_notes.append("Same note every time")

            with patch.dict(
                os.environ,
                {
                    AUTOSAVE_RESEARCH_NOTE_FILE_ENV: "notes/research.md",
                    AUTOSAVE_VALIDATION_SUMMARY_FILE_ENV: "notes/validation.txt",
                },
                clear=False,
            ):
                artifacts = apply_capture_options(repo_root, session, options=CaptureOptions())

            self.assertEqual(session.research_notes, ["Same note every time"])
            self.assertEqual(session.validation.summary, "Validation is pending")
            self.assertEqual(
                artifacts["research_note_source"],
                str((notes_dir / "research.md").resolve()),
            )
            self.assertEqual(
                artifacts["validation_summary_source"],
                str((notes_dir / "validation.txt").resolve()),
            )

    @skipUnless(shutil.which("git"), "git is required for git capture tests")
    def test_capture_git_touched_files_ignores_agent_relay_internal_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)

            (repo_root / ".agent-relay").mkdir()
            (repo_root / ".agent-relay" / "state.json").write_text("{}", encoding="utf-8")
            (repo_root / "src").mkdir()
            (repo_root / "src" / "demo.py").write_text("print('demo')\n", encoding="utf-8")

            touched_files = capture_git_touched_files(repo_root)

            self.assertIn("src/demo.py", touched_files)
            self.assertNotIn(".agent-relay/state.json", touched_files)
