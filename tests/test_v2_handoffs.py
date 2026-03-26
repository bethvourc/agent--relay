from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.capture import CaptureOptions
from agent_relay.v2.checkpoints import create_checkpoint_for_command
from agent_relay.v2.handoffs import (
    create_handoff_for_command,
    execute_launch_for_command,
    preview_launch_for_command,
    recover_interrupted_launches,
    resume_handoff_for_command,
)
from agent_relay.v2.layout import object_dir
from agent_relay.v2.storage import load_session_view
from agent_relay.v2.tx import JournalCommitRequest, SessionTransaction
from tests.v2_fixtures import build_sample_v2_session


class V2HandoffTests(TestCase):
    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "relay@example.com"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Relay Test"], cwd=repo_root, check=True, capture_output=True, text=True)

    def commit_file(self, repo_root: Path, relative_path: str, content: str, message: str) -> None:
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(["git", "add", relative_path], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True, capture_output=True, text=True)

    def prepare_session(self, repo_root: Path) -> dict[str, str]:
        self.init_git_repo(repo_root)
        self.commit_file(repo_root, "src/app.py", "print('hello')\n", "initial commit")
        fixture = build_sample_v2_session(repo_root)
        create_checkpoint_for_command(
            repo_root,
            fixture["session_id"],
            command_name="prepare",
            options=CaptureOptions(next_action="Prepare an immutable handoff"),
            owner="test:prepare",
        )
        return fixture

    def test_same_target_repeated_failovers_remain_immutable_and_supersede_old_handoffs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)

            first = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="First immutable handoff",
                evidence_depth="standard",
                owner="test:handoff:first",
            )
            second = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Second immutable handoff",
                evidence_depth="standard",
                owner="test:handoff:second",
            )

            view = load_session_view(repo_root, fixture["session_id"])

            self.assertNotEqual(first.handoff_id, second.handoff_id)
            self.assertNotEqual(first.resume_path, second.resume_path)
            self.assertTrue(Path(first.resume_path).exists())
            self.assertTrue(Path(second.resume_path).exists())
            self.assertEqual(view.prepared_handoff_id, second.handoff_id)

            with self.assertRaises(SystemExit) as context:
                preview_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=first.handoff_id,
                    owner="test:preview:old",
                )

            self.assertIn("superseded", str(context.exception))

    def test_stale_handoff_is_rejected_after_newer_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            handoff = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Prepare a stale handoff",
                evidence_depth="standard",
                owner="test:handoff:stale",
            )

            create_checkpoint_for_command(
                repo_root,
                fixture["session_id"],
                command_name="checkpoint",
                options=CaptureOptions(next_action="Newer checkpoint supersedes the handoff"),
                owner="test:checkpoint:newer",
            )

            with self.assertRaises(SystemExit) as context:
                preview_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=handoff.handoff_id,
                    owner="test:preview:stale",
                )

            self.assertIn("stale", str(context.exception))

    def test_missing_packet_is_rejected_as_corruption(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            handoff = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Packet corruption check",
                evidence_depth="standard",
                owner="test:handoff:corrupt",
            )
            Path(handoff.resume_path).unlink()

            with self.assertRaises(SystemExit) as context:
                preview_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=handoff.handoff_id,
                    owner="test:preview:corrupt",
                )

            self.assertIn("launch is blocked while session health is degraded", str(context.exception))
            self.assertIn("--promote-last-good", str(context.exception))

    def test_failed_launch_captures_immutable_stdout_and_stderr_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            env = {
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"{shlex_quote(sys.executable)} -c "
                    "\"import sys; print('phase4 stdout'); print('phase4 stderr', file=sys.stderr); sys.exit(7)\""
                )
            }

            with patch.dict(os.environ, env, clear=False):
                handoff = create_handoff_for_command(
                    repo_root,
                    fixture["session_id"],
                    to_agent="claude",
                    reason="Capture launch logs",
                    evidence_depth="standard",
                    owner="test:handoff:logs",
                )
                result = execute_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=handoff.handoff_id,
                    owner="test:launch:logs",
                )

            view = load_session_view(repo_root, fixture["session_id"])
            stdout_text = Path(result.stdout_path).read_text(encoding="utf-8")
            stderr_text = Path(result.stderr_path).read_text(encoding="utf-8")

            self.assertEqual(result.launch_status, "failed")
            self.assertEqual(result.exit_code, 7)
            self.assertEqual(view.phase, "ready_for_handoff")
            self.assertEqual(view.prepared_handoff_id, handoff.handoff_id)
            self.assertEqual(view.latest_launch_id, result.launch_id)
            self.assertIn("phase4 stdout", stdout_text)
            self.assertIn("phase4 stderr", stderr_text)

    def test_interrupted_launch_recovery_writes_interrupted_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            handoff = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Recover an interrupted launch",
                evidence_depth="standard",
                owner="test:handoff:recover",
            )
            launch_id = "la-20260325T181500Z-999999"

            with SessionTransaction.begin(
                repo_root,
                fixture["session_id"],
                operation="launch.started",
                owner="test:launch:started",
            ) as tx:
                tx.commit(
                    JournalCommitRequest(
                        event_type="launch.started",
                        phase_before="ready_for_handoff",
                        phase_after="launching",
                        payload={"handoff_id": handoff.handoff_id, "launch_id": launch_id},
                        timestamp="2026-03-25T18:15:00Z",
                    )
                )

            recovered_launch_id = recover_interrupted_launches(
                repo_root,
                fixture["session_id"],
                owner="test:launch:recover",
            )
            view = load_session_view(repo_root, fixture["session_id"])
            stderr_path = object_dir(repo_root, fixture["session_id"], "launch", launch_id) / "stderr.log"

            self.assertEqual(recovered_launch_id, launch_id)
            self.assertEqual(view.phase, "ready_for_handoff")
            self.assertEqual(view.latest_launch_id, launch_id)
            self.assertEqual(view.prepared_handoff_id, handoff.handoff_id)
            self.assertEqual(view.handoffs[-1].launch_status, "interrupted")
            self.assertIn("recovered by Agent Relay", stderr_path.read_text(encoding="utf-8"))

    def test_resume_rejects_superseded_handoff_and_accepts_current_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            first = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Superseded handoff",
                evidence_depth="standard",
                owner="test:handoff:superseded:first",
            )
            second = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Current handoff",
                evidence_depth="standard",
                owner="test:handoff:superseded:second",
            )

            with self.assertRaises(SystemExit) as context:
                resume_handoff_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=first.handoff_id,
                    owner="test:resume:old",
                )

            self.assertIn("superseded", str(context.exception))

            result = resume_handoff_for_command(
                repo_root,
                fixture["session_id"],
                handoff_id=second.handoff_id,
                owner="test:resume:current",
            )
            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(result.current_agent, "claude")
            self.assertEqual(view.current_agent, "claude")
            self.assertEqual(view.phase, "active")
            self.assertEqual(view.last_resume_handoff_id, second.handoff_id)

    def test_launch_is_rejected_while_awaiting_resume(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            env = {
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"{shlex_quote(sys.executable)} -c "
                    "\"print('launch once')\""
                )
            }

            with patch.dict(os.environ, env, clear=False):
                handoff = create_handoff_for_command(
                    repo_root,
                    fixture["session_id"],
                    to_agent="claude",
                    reason="Reach awaiting_resume",
                    evidence_depth="standard",
                    owner="test:handoff:awaiting-resume",
                )
                result = execute_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=handoff.handoff_id,
                    owner="test:launch:awaiting-resume",
                )

            self.assertEqual(result.launch_status, "succeeded")

            with self.assertRaises(SystemExit) as context:
                preview_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=handoff.handoff_id,
                    owner="test:launch:awaiting-resume:preview",
                )

            self.assertIn("launch is not allowed while session phase is awaiting_resume", str(context.exception))


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
