from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agent_relay.capture_support import CaptureOptions
from agent_relay.checkpoints import create_checkpoint_for_command
from agent_relay.handoffs import create_handoff_for_command
from agent_relay.lifecycle import (
    LifecycleState,
    LifecycleViolation,
    plan_checkpoint_command,
    plan_complete_command,
    plan_failover_command,
    plan_inspect_command,
    plan_launch_finished,
    plan_launch_started,
    plan_repair_command,
    plan_resume_command,
    plan_session_started,
)
from agent_relay.tx import JournalCommitRequest, SessionTransaction
from tests.v2_fixtures import build_sample_v2_session


class V2LifecycleTests(TestCase):
    def init_git_repo(self, repo_root: Path) -> None:
        subprocess.run(["git", "init"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.email", "relay@example.com"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "config", "user.name", "Relay Test"], cwd=repo_root, check=True, capture_output=True, text=True)
        (repo_root / "src").mkdir()
        (repo_root / "src" / "demo.py").write_text("print('demo')\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/demo.py"], cwd=repo_root, check=True, capture_output=True, text=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, check=True, capture_output=True, text=True)

    def test_session_started_transition_is_canonical(self) -> None:
        transition = plan_session_started()

        self.assertIsNone(transition.phase_before)
        self.assertEqual(transition.phase_after, "active")
        self.assertIsNone(transition.task_status_after)

    def test_checkpoint_transitions_keep_phase_and_task_status_separate(self) -> None:
        paused = plan_checkpoint_command(
            LifecycleState(phase="paused", task_status="blocked"),
            command_name="checkpoint",
        )
        activate = plan_checkpoint_command(
            LifecycleState(phase="paused", task_status="blocked"),
            command_name="checkpoint",
            status_directive="active",
        )
        block = plan_checkpoint_command(
            LifecycleState(phase="active", task_status="working"),
            command_name="checkpoint",
            status_directive="blocked",
        )
        done = plan_checkpoint_command(
            LifecycleState(phase="active", task_status="working"),
            command_name="checkpoint",
            status_directive="done",
        )
        pause = plan_checkpoint_command(
            LifecycleState(phase="active", task_status=None),
            command_name="pause",
        )
        prepare = plan_checkpoint_command(
            LifecycleState(phase="paused", task_status=None),
            command_name="prepare",
        )

        self.assertEqual(paused.phase_after, "paused")
        self.assertEqual(paused.task_status_after, "blocked")
        self.assertEqual(activate.phase_after, "active")
        self.assertEqual(activate.task_status_after, "working")
        self.assertEqual(block.phase_after, "active")
        self.assertEqual(block.task_status_after, "blocked")
        self.assertEqual(done.phase_after, "active")
        self.assertEqual(done.task_status_after, "done")
        self.assertEqual(pause.phase_after, "paused")
        self.assertEqual(pause.task_status_after, "working")
        self.assertEqual(prepare.phase_after, "ready_for_handoff")
        self.assertEqual(prepare.task_status_after, "working")

    def test_invalid_transitions_are_explicit(self) -> None:
        with self.assertRaises(LifecycleViolation) as prepare_error:
            plan_checkpoint_command(
                LifecycleState(phase="completed", task_status="done"),
                command_name="prepare",
            )
        with self.assertRaises(LifecycleViolation) as failover_error:
            plan_failover_command(LifecycleState(phase="active", task_status="working"))
        with self.assertRaises(LifecycleViolation) as launch_error:
            plan_launch_started(LifecycleState(phase="awaiting_resume", task_status="working"))
        with self.assertRaises(LifecycleViolation) as resume_error:
            plan_resume_command(LifecycleState(phase="active", task_status="working"))
        with self.assertRaises(LifecycleViolation) as complete_error:
            plan_complete_command(LifecycleState(phase="ready_for_handoff", task_status="working"))
        with self.assertRaises(LifecycleViolation) as override_error:
            plan_checkpoint_command(
                LifecycleState(phase="active", task_status="working"),
                command_name="prepare",
                status_directive="blocked",
            )

        self.assertIn("allowed phases", str(prepare_error.exception))
        self.assertIn("failover is not allowed", str(failover_error.exception))
        self.assertIn("launch is not allowed", str(launch_error.exception))
        self.assertIn("resume is not allowed", str(resume_error.exception))
        self.assertIn("complete is not allowed", str(complete_error.exception))
        self.assertIn("does not accept a status override", str(override_error.exception))

    def test_phase_preserving_commands_and_launch_finish_are_explicit(self) -> None:
        state = LifecycleState(phase="launching", task_status="blocked")

        inspect = plan_inspect_command(state)
        repair = plan_repair_command(state)
        failed_launch = plan_launch_finished(state, launch_status="failed")
        successful_launch = plan_launch_finished(state, launch_status="succeeded")

        self.assertEqual(inspect.phase_after, "launching")
        self.assertEqual(repair.phase_after, "launching")
        self.assertEqual(failed_launch.phase_after, "ready_for_handoff")
        self.assertEqual(successful_launch.phase_after, "awaiting_resume")
        self.assertEqual(successful_launch.task_status_after, "blocked")

    def test_completed_sessions_reject_prepare_and_failover_through_domain_commands(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            self.init_git_repo(repo_root)
            fixture = build_sample_v2_session(repo_root)

            with SessionTransaction.begin(
                repo_root,
                fixture["session_id"],
                operation="session.completed",
                owner="test:lifecycle:complete",
            ) as tx:
                tx.commit(
                    JournalCommitRequest(
                        event_type="session.completed",
                        phase_before="active",
                        phase_after="completed",
                        payload={"completed_by_agent": "codex"},
                        timestamp="2026-03-25T18:15:00Z",
                    )
                )

            with self.assertRaises(SystemExit) as prepare_error:
                create_checkpoint_for_command(
                    repo_root,
                    fixture["session_id"],
                    command_name="prepare",
                    options=CaptureOptions(next_action="Should not be accepted"),
                    owner="test:lifecycle:prepare",
                )

            with self.assertRaises(SystemExit) as failover_error:
                create_handoff_for_command(
                    repo_root,
                    fixture["session_id"],
                    to_agent="claude",
                    reason="Should not be accepted",
                    evidence_depth="standard",
                    owner="test:lifecycle:failover",
                )

            self.assertIn("prepare is not allowed while session phase is completed", str(prepare_error.exception))
            self.assertIn("failover is not allowed while session phase is completed", str(failover_error.exception))
