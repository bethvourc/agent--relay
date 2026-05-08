from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.capture_support import CaptureOptions
from agent_relay.checkpoints import create_checkpoint_for_command
from agent_relay.handoffs import (
    create_handoff_for_command,
    execute_launch_for_command,
    preview_launch_for_command,
    recover_interrupted_launches,
    resume_handoff_for_command,
)
from agent_relay.layout import object_dir, pending_tx_dir, turn_dir, workspace_log_path
from agent_relay.storage import load_session_view
from agent_relay.tx import JournalCommitRequest, SessionTransaction, recover_session_transactions
from agent_relay.workspace_log import LogEntry, WorkspaceLog
from tests.session_fixtures import build_sample_session


class HandoffTests(TestCase):
    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "config", "user.email", "relay@example.com"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "Relay Test"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

    def commit_file(self, repo_root: Path, relative_path: str, content: str, message: str) -> None:
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        subprocess.run(
            ["git", "add", relative_path], cwd=repo_root, check=True, capture_output=True, text=True
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )

    def prepare_session(self, repo_root: Path) -> dict[str, str]:
        self.init_git_repo(repo_root)
        self.commit_file(repo_root, "src/app.py", "print('hello')\n", "initial commit")
        fixture = build_sample_session(repo_root)
        create_checkpoint_for_command(
            repo_root,
            fixture["session_id"],
            command_name="prepare",
            options=CaptureOptions(next_action="Prepare an immutable handoff"),
            owner="test:prepare",
        )
        return fixture

    def write_conversation_artifacts(self, repo_root: Path, session_id: str) -> None:
        first_turn = turn_dir(repo_root, session_id, 1)
        first_turn.mkdir(parents=True, exist_ok=True)
        (first_turn / "prompt.md").write_text("Turn 1 prompt\n", encoding="utf-8")
        (first_turn / "output.jsonl").write_text(
            '{"message":{"role":"assistant","content":[{"type":"text","text":"Reviewed the current relay handoff flow."}]}}\n',
            encoding="utf-8",
        )
        (first_turn / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "relay_resumable_state",
                    "source": "relay_turn",
                    "summary": "Reviewed the current relay handoff flow.",
                    "next_step": "Capture the missing planning state.",
                    "current_plan": ["Capture the missing planning state."],
                    "verification": ["Review recent relay artifacts"],
                    "agent_key": "claude",
                    "turn_number": 1,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        second_turn = turn_dir(repo_root, session_id, 2)
        second_turn.mkdir(parents=True, exist_ok=True)
        (second_turn / "prompt.md").write_text("Turn 2 prompt\n", encoding="utf-8")
        (second_turn / "output.jsonl").write_text(
            '{"type":"item.completed","item":{"type":"agent_message","text":"Identified that hidden planning state is not captured yet."}}\n',
            encoding="utf-8",
        )
        (second_turn / "stderr.log").write_text("warning: rate limit soon\n", encoding="utf-8")
        (second_turn / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "relay_resumable_state",
                    "source": "relay_turn",
                    "summary": "Identified that hidden planning state is not captured yet.",
                    "next_step": "Persist resumable state in handoff artifacts.",
                    "remaining_work": ["Persist resumable state in handoff artifacts."],
                    "intended_edits": ["src/agent_relay/handoffs.py"],
                    "agent_key": "codex",
                    "turn_number": 2,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        workspace_log = WorkspaceLog(workspace_log_path(repo_root, session_id))
        workspace_log.append(
            LogEntry(
                timestamp="2026-03-25T18:09:00Z",
                agent_key="claude",
                agent_slot=0,
                entry_type="turn_complete",
                summary="Reviewed the current relay handoff flow.",
            )
        )
        workspace_log.append(
            LogEntry(
                timestamp="2026-03-25T18:10:00Z",
                agent_key="codex",
                agent_slot=1,
                entry_type="turn_complete",
                summary="Identified that hidden planning state is not captured yet.",
            )
        )

    def test_same_target_repeated_failovers_remain_immutable_and_supersede_old_handoffs(
        self,
    ) -> None:
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

    def test_standard_handoff_bundles_recent_relay_conversation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            self.write_conversation_artifacts(repo_root, fixture["session_id"])

            handoff = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Bundle recent relay context",
                evidence_depth="standard",
                owner="test:handoff:relay-context",
            )

            packet_text = Path(handoff.resume_path).read_text(encoding="utf-8")
            handoff_dir = object_dir(
                repo_root, fixture["session_id"], "handoff", handoff.handoff_id
            )

            self.assertIn("## Resumable State", packet_text)
            self.assertIn("Persist resumable state in handoff artifacts.", packet_text)
            self.assertIn("relay/turns/turn-002/state.json", packet_text)
            self.assertIn("## Prior Relay Conversation", packet_text)
            self.assertIn("Turn 1: Reviewed the current relay handoff flow.", packet_text)
            self.assertIn(
                "Turn 2: Identified that hidden planning state is not captured yet.", packet_text
            )
            self.assertIn("relay/turns/turn-001/output.jsonl", packet_text)
            self.assertIn("relay/workspace-log.md", packet_text)
            self.assertTrue((handoff_dir / "relay" / "turns" / "turn-001" / "prompt.md").exists())
            self.assertTrue(
                (handoff_dir / "relay" / "turns" / "turn-002" / "output.jsonl").exists()
            )
            self.assertTrue((handoff_dir / "relay" / "turns" / "turn-002" / "state.json").exists())
            self.assertTrue((handoff_dir / "relay" / "workspace-log.md").exists())

    def test_minimal_handoff_skips_relay_conversation_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            self.write_conversation_artifacts(repo_root, fixture["session_id"])

            handoff = create_handoff_for_command(
                repo_root,
                fixture["session_id"],
                to_agent="claude",
                reason="Keep packet minimal",
                evidence_depth="minimal",
                owner="test:handoff:minimal-relay-context",
            )

            packet_text = Path(handoff.resume_path).read_text(encoding="utf-8")
            handoff_dir = object_dir(
                repo_root, fixture["session_id"], "handoff", handoff.handoff_id
            )

            self.assertNotIn("## Prior Relay Conversation", packet_text)
            self.assertFalse((handoff_dir / "relay").exists())

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

            self.assertIn(
                "launch is blocked while session health is degraded", str(context.exception)
            )
            self.assertIn("--promote-last-good", str(context.exception))

    def test_failed_launch_captures_immutable_stdout_and_stderr_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            env = {
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"{shlex_quote(sys.executable)} -c "
                    "\"import sys; print('phase4 stdout'); print('phase4 stderr', file=sys.stderr); sys.exit(7)\" "
                    "{resume_path}"
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
            stderr_path = (
                object_dir(repo_root, fixture["session_id"], "launch", launch_id) / "stderr.log"
            )

            self.assertEqual(recovered_launch_id, launch_id)
            self.assertEqual(view.phase, "ready_for_handoff")
            self.assertEqual(view.latest_launch_id, launch_id)
            self.assertEqual(view.prepared_handoff_id, handoff.handoff_id)
            self.assertEqual(view.handoffs[-1].launch_status, "interrupted")
            self.assertIn("recovered by Agent Relay", stderr_path.read_text(encoding="utf-8"))

    def test_interrupted_failover_is_not_visible_until_transaction_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            handoff_id = "ho-20260325T181550Z-bbbbbb"
            interrupted_path: Path | None = None

            import agent_relay.tx as tx_module

            original_write_json_atomic = tx_module.write_json_atomic

            def interrupt_on_journal_write(path: Path, payload) -> None:
                nonlocal interrupted_path
                if (
                    path.parent.name == "journal"
                    and isinstance(payload, dict)
                    and payload.get("kind") == "journal_event"
                ):
                    interrupted_path = path
                    raise KeyboardInterrupt("simulated interruption before handoff journal commit")
                original_write_json_atomic(path, payload)

            with patch("agent_relay.handoffs.handoff_id_now", return_value=handoff_id):
                with patch(
                    "agent_relay.tx.write_json_atomic", side_effect=interrupt_on_journal_write
                ):
                    with self.assertRaises(KeyboardInterrupt):
                        create_handoff_for_command(
                            repo_root,
                            fixture["session_id"],
                            to_agent="claude",
                            reason="Interrupt failover",
                            evidence_depth="standard",
                            owner="test:handoff:interrupt",
                        )

            view = load_session_view(repo_root, fixture["session_id"])
            handoff_dir = object_dir(repo_root, fixture["session_id"], "handoff", handoff_id)

            self.assertIsNotNone(interrupted_path)
            self.assertFalse(interrupted_path.exists())
            self.assertIsNone(view.prepared_handoff_id)
            self.assertTrue(handoff_dir.exists())
            self.assertTrue(
                any(
                    path.is_dir()
                    for path in pending_tx_dir(repo_root, fixture["session_id"]).iterdir()
                )
            )

            report = recover_session_transactions(repo_root, fixture["session_id"])
            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(report.quarantined_transactions, 1)
            self.assertFalse(handoff_dir.exists())
            self.assertIsNone(view.prepared_handoff_id)

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
                    f"{shlex_quote(sys.executable)} -c \"print('launch once')\" {{resume_path}}"
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

            self.assertIn(
                "launch is not allowed while session phase is awaiting_resume",
                str(context.exception),
            )

    def test_unsafe_launch_template_is_preview_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            env = {
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"{shlex_quote(sys.executable)} -c \"print('unsafe template')\""
                )
            }

            with patch.dict(os.environ, env, clear=False):
                handoff = create_handoff_for_command(
                    repo_root,
                    fixture["session_id"],
                    to_agent="claude",
                    reason="Unsafe template preview",
                    evidence_depth="standard",
                    owner="test:handoff:unsafe-template",
                )
                preview = preview_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=handoff.handoff_id,
                    owner="test:launch:unsafe-template:preview",
                )

                self.assertFalse(preview.packet_aware)
                self.assertEqual(preview.execute_policy, "refuse")
                self.assertIn("does not pass the resume packet", preview.warning or "")

                with self.assertRaises(SystemExit) as context:
                    execute_launch_for_command(
                        repo_root,
                        fixture["session_id"],
                        handoff_id=handoff.handoff_id,
                        owner="test:launch:unsafe-template:execute",
                    )

            self.assertIn("does not pass the resume packet", str(context.exception))

    def test_interrupted_resume_does_not_transfer_ownership_until_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            fixture = self.prepare_session(repo_root)
            before_resume = load_session_view(repo_root, fixture["session_id"])
            env = {
                "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": (
                    f"{shlex_quote(sys.executable)} -c \"print('launch once')\" {{resume_path}}"
                )
            }

            with patch.dict(os.environ, env, clear=False):
                handoff = create_handoff_for_command(
                    repo_root,
                    fixture["session_id"],
                    to_agent="claude",
                    reason="Interrupt resume",
                    evidence_depth="standard",
                    owner="test:handoff:interrupt-resume",
                )
                result = execute_launch_for_command(
                    repo_root,
                    fixture["session_id"],
                    handoff_id=handoff.handoff_id,
                    owner="test:launch:interrupt-resume",
                )

            self.assertEqual(result.launch_status, "succeeded")
            interrupted_path: Path | None = None

            import agent_relay.tx as tx_module

            original_write_json_atomic = tx_module.write_json_atomic

            def interrupt_on_journal_write(path: Path, payload) -> None:
                nonlocal interrupted_path
                if (
                    path.parent.name == "journal"
                    and isinstance(payload, dict)
                    and payload.get("kind") == "journal_event"
                ):
                    interrupted_path = path
                    raise KeyboardInterrupt("simulated interruption before resume journal commit")
                original_write_json_atomic(path, payload)

            with patch("agent_relay.tx.write_json_atomic", side_effect=interrupt_on_journal_write):
                with self.assertRaises(KeyboardInterrupt):
                    resume_handoff_for_command(
                        repo_root,
                        fixture["session_id"],
                        handoff_id=handoff.handoff_id,
                        owner="test:resume:interrupt",
                    )

            view = load_session_view(repo_root, fixture["session_id"])

            self.assertIsNotNone(interrupted_path)
            self.assertFalse(interrupted_path.exists())
            self.assertEqual(view.current_agent, "codex")
            self.assertEqual(view.phase, "awaiting_resume")
            self.assertEqual(view.last_resume_handoff_id, before_resume.last_resume_handoff_id)
            self.assertTrue(
                any(
                    path.is_dir()
                    for path in pending_tx_dir(repo_root, fixture["session_id"]).iterdir()
                )
            )

            report = recover_session_transactions(repo_root, fixture["session_id"])
            view = load_session_view(repo_root, fixture["session_id"])

            self.assertEqual(report.quarantined_transactions, 1)
            self.assertEqual(view.current_agent, "codex")
            self.assertEqual(view.phase, "awaiting_resume")
            self.assertEqual(view.last_resume_handoff_id, before_resume.last_resume_handoff_id)


def shlex_quote(value: str) -> str:
    import shlex

    return shlex.quote(value)
