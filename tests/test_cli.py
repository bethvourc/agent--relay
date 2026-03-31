from __future__ import annotations

import json
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from agent_relay.cli import (
    _open_tmux_session_in_terminal,
    _race_result_metadata,
    _should_auto_open_terminals,
    build_parser,
)
from agent_relay.concurrent import AgentOutcome, ConcurrentResult
from agent_relay.ui import create_console


ROOT = Path(__file__).resolve().parents[1]


class AgentRelayCliTests(TestCase):
    def run_cli(self, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = {
            "PYTHONPATH": str(ROOT / "src"),
        }
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "agent_relay.cli", *args],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )

    def run_cli_json(self, *args: str, extra_env: dict[str, str] | None = None) -> dict:
        result = self.run_cli("--json", *args, extra_env=extra_env)
        return json.loads(result.stdout)

    def start_snapshot_session(
        self,
        tmpdir: str,
        *,
        agent: str,
        task: str,
        extra_env: dict[str, str] | None = None,
    ) -> dict:
        return self.run_cli_json(
            "start",
            "--agent",
            agent,
            "--task",
            task,
            "--snapshot-mode",
            "full",
            "--repo",
            tmpdir,
            extra_env=extra_env,
        )

    def write_conflict_artifact(
        self,
        repo_root: Path,
        *,
        session_id: str,
        payload: dict,
        extra_files: dict[str, str] | None = None,
    ) -> Path:
        artifact_dir = repo_root / ".agent-relay" / "sessions" / session_id / "concurrent"
        (artifact_dir / "conflicts").mkdir(parents=True, exist_ok=True)
        if extra_files:
            for relative_path, content in extra_files.items():
                target = artifact_dir / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
        artifact_path = artifact_dir / "conflicts.json"
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")
        return artifact_path

    def test_start_creates_initial_state_checkpoint_and_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            data = self.start_snapshot_session(tmpdir, agent="claude", task="Draft the handoff schema")
            session_id = data["session_id"]
            self.assertEqual(data["command"], "start")
            self.assertEqual(data["agent"], "claude")
            self.assertEqual(data["status"], "active")
            self.assertEqual(data["storage_model"], "journal_v2")

            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            manifest_path = session_root / "session.json"
            journal_events = list((session_root / "journal").glob("*.json"))
            checkpoint_root = session_root / "objects" / "checkpoints" / data["checkpoint_id"]
            summary = checkpoint_root / "summary.md"

            self.assertTrue(manifest_path.exists())
            self.assertTrue(summary.exists())
            self.assertEqual(len(journal_events), 2)

            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            self.assertEqual(inspect["current_agent"], "claude")
            self.assertEqual(inspect["objective"], "Draft the handoff schema")
            self.assertEqual(inspect["current_status"], "active")
            self.assertEqual(inspect["latest_checkpoint_id"], data["checkpoint_id"])
            self.assertIn("Checkpoint ID:", summary.read_text())

    def test_checkpoint_creates_new_checkpoint_and_updates_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Draft the handoff schema")
            session_id = start_data["session_id"]
            session_root = repo_root / ".agent-relay" / "sessions" / session_id

            cp_data = self.run_cli_json(
                "checkpoint",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Render a Codex resume packet",
                "--decision",
                "Use repo-local state",
                "--touched-file",
                "src/agent_relay/cli.py",
                "--repo",
                tmpdir,
            )

            checkpoints = list((session_root / "objects" / "checkpoints").glob("*"))
            self.assertEqual(len(checkpoints), 2)
            latest_checkpoint_id = cp_data["checkpoint_id"]
            self.assertEqual(cp_data["command"], "checkpoint")
            self.assertIn(latest_checkpoint_id, {path.name for path in checkpoints})

            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            summary = (session_root / "objects" / "checkpoints" / latest_checkpoint_id / "summary.md").read_text()

            self.assertEqual(inspect["latest_checkpoint_id"], latest_checkpoint_id)
            self.assertIn("Render a Codex resume packet", summary)
            self.assertIn("Use repo-local state", summary)
            self.assertIn("src/agent_relay/cli.py", summary)

    def test_inspect_conflicts_returns_json_summary(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            artifact_path = self.write_conflict_artifact(
                repo_root,
                session_id="sess-123",
                payload={
                    "session_id": "sess-123",
                    "status": "manual_resolution_required",
                    "note": "Need a human decision.",
                    "manual_paths": ["assets/logo.bin"],
                    "attempted_slots": [0, 1],
                    "paths": [
                        {
                            "path": "README.md",
                            "manual_reasons": ["lockfile"],
                            "base_version": {"exists": True, "path": "conflicts/base/README.md"},
                            "repo_version": {"exists": True, "path": "conflicts/repo/README.md"},
                            "contributors": [
                                {
                                    "slot": 0,
                                    "agent": "claude",
                                    "claim_specs": [{"path": "README.md", "role": "shared"}],
                                    "version_path": "conflicts/slot-00/README.md",
                                },
                            ],
                        }
                    ],
                },
                extra_files={
                    "conflicts/base/README.md": "base\n",
                    "conflicts/repo/README.md": "repo\n",
                    "conflicts/slot-00/README.md": "slot\n",
                },
            )
            data = self.run_cli_json("inspect-conflicts", "sess-123", "--repo", tmpdir)
            self.assertEqual(data["command"], "inspect-conflicts")
            self.assertEqual(data["session_id"], "sess-123")
            self.assertEqual(data["status"], "manual_resolution_required")
            self.assertEqual(data["conflict_artifact_path"], str(artifact_path))
            self.assertEqual(data["manual_paths"], ["assets/logo.bin"])
            self.assertEqual(data["attempted_slots"], [0, 1])
            self.assertEqual(data["paths"][0]["path"], "README.md")
            self.assertEqual(data["paths"][0]["kind"], "text")
            self.assertEqual(data["paths"][0]["manual_reasons"], ["lockfile"])
            self.assertEqual(data["paths"][0]["contributors"][0]["roles"], ["shared"])

    def test_checkpoint_captures_notes_validation_and_git_touched_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            subprocess.run(["git", "init"], cwd=repo_root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=repo_root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Agent Relay Tests"], cwd=repo_root, text=True, capture_output=True, check=True)

            notes_dir = repo_root / "notes"
            notes_dir.mkdir()
            (notes_dir / "implementation.md").write_text("Added the Phase 6 capture flow\n")
            (notes_dir / "validation.txt").write_text("Manual verification still pending\n")
            (repo_root / "src").mkdir()
            (repo_root / "src" / "phase6.py").write_text("print('phase6')\n")
            subprocess.run(["git", "add", "."], cwd=repo_root, text=True, capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=repo_root, text=True, capture_output=True, check=True)
            (repo_root / "src" / "phase6.py").write_text("print('phase6 updated')\n", encoding="utf-8")

            start_data = self.run_cli_json(
                "start",
                "--agent",
                "claude",
                "--task",
                "Capture richer checkpoint state",
                "--repo",
                tmpdir,
            )
            session_id = start_data["session_id"]

            cp_data = self.run_cli_json(
                "checkpoint",
                session_id,
                "--next-action",
                "Prepare the final handoff",
                "--research-note",
                "Investigated the last safe checkpoint flow",
                "--implementation-note-file",
                "notes/implementation.md",
                "--validation-status",
                "partial",
                "--validation-summary-file",
                "notes/validation.txt",
                "--capture-git-changes",
                "--repo",
                tmpdir,
            )

            session_root = repo_root / ".agent-relay" / "sessions" / session_id
            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            summary = (session_root / "objects" / "checkpoints" / cp_data["checkpoint_id"] / "summary.md").read_text()

            self.assertEqual(cp_data["status"], "active")
            self.assertEqual(inspect["validation"]["status"], "partial")
            self.assertEqual(inspect["validation"]["summary"], "Manual verification still pending")
            self.assertIn("Investigated the last safe checkpoint flow", inspect["research_notes"])
            self.assertIn("Added the Phase 6 capture flow", inspect["implementation_notes"])
            self.assertIn("src/phase6.py", inspect["touched_files"])
            self.assertTrue(all(not path.startswith(".agent-relay/") for path in inspect["touched_files"]))
            self.assertIn("Task status:", summary)
            self.assertIn("Added the Phase 6 capture flow", summary)

    def test_failover_writes_resume_packet_and_records_checkpoint_reference(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Draft the handoff schema")
            session_id = start_data["session_id"]

            prepare = self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Render a Codex resume packet",
                "--decision",
                "Use repo-local state",
                "--repo",
                tmpdir,
            )

            fo_data = self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "claude rate limit reached",
                "--repo",
                tmpdir,
            )

            resume_path = Path(fo_data["resume_path"])
            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            handoff = inspect["handoffs"][0]

            self.assertTrue(resume_path.exists())
            self.assertEqual(inspect["current_status"], "ready_for_handoff")
            self.assertEqual(handoff["to_agent"], "codex")
            self.assertEqual(handoff["checkpoint_id"], prepare["checkpoint_id"])
            self.assertIn("# Codex Resume Packet", resume_path.read_text())
            preview = self.run_cli_json("launch", session_id, "--repo", tmpdir)
            self.assertEqual(fo_data["launch_command"], preview["launch_command"])

    def test_failover_uses_profile_specific_launch_template_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="codex", task="Hand work back to Claude Code")
            session_id = start_data["session_id"]

            self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Use the Claude Code resume packet",
                "--repo",
                tmpdir,
            )

            fo_data = self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "claude",
                "--reason",
                "switching agents for review",
                "--repo",
                tmpdir,
                extra_env={
                    "AGENT_RELAY_CLAUDE_LAUNCH_TEMPLATE": "cd {repo_root} && {agent_cli} --resume {resume_path}",
                },
            )

            resume_path = Path(fo_data["resume_path"])
            expected_command = (
                f"cd {shlex.quote(str(repo_root))} && claude --resume {shlex.quote(str(resume_path))}"
            )

            self.assertIn("# Claude Code Resume Packet", resume_path.read_text())
            self.assertEqual(fo_data["launch_command"], expected_command)

    def test_launch_dry_run_prints_latest_handoff_without_mutating_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Prepare a dry-run launch")
            session_id = start_data["session_id"]

            self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Ready to hand off",
                "--repo",
                tmpdir,
            )

            failover = self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "ready to hand off",
                "--repo",
                tmpdir,
            )

            launch_data = self.run_cli_json(
                "launch",
                session_id,
                "--repo",
                tmpdir,
            )

            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            handoff = inspect["handoffs"][0]

            self.assertEqual(launch_data["command"], "launch")
            self.assertEqual(launch_data["mode"], "dry_run")
            self.assertEqual(launch_data["session_id"], session_id)
            self.assertEqual(launch_data["target"], "codex")
            self.assertEqual(launch_data["launch_command"], failover["launch_command"])
            self.assertEqual(launch_data["launch_instructions"], failover["launch_instructions"])
            self.assertEqual(handoff["launch_status"], "ready")
            self.assertEqual(inspect["current_agent"], "claude")

    def test_prepare_creates_ready_for_handoff_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Prepare a clean handoff")
            session_id = start_data["session_id"]
            session_root = repo_root / ".agent-relay" / "sessions" / session_id

            prepare_data = self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Hand off to Codex for continuation",
                "--decision",
                "Pause before preparing the target packet",
                "--repo",
                tmpdir,
            )

            checkpoints = list((session_root / "objects" / "checkpoints").glob("*"))
            inspect = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            summary = (session_root / "objects" / "checkpoints" / prepare_data["checkpoint_id"] / "summary.md").read_text()

            self.assertEqual(len(checkpoints), 2)
            self.assertEqual(prepare_data["command"], "prepare")
            self.assertEqual(prepare_data["status"], "ready_for_handoff")
            self.assertEqual(inspect["current_status"], "ready_for_handoff")
            self.assertEqual(inspect["next_action"], "Hand off to Codex for continuation")
            self.assertEqual(inspect["latest_checkpoint_id"], prepare_data["checkpoint_id"])
            self.assertIn("Pause before preparing the target packet", inspect["decisions"])
            self.assertIn("Phase: ready_for_handoff", summary)
            self.assertIn("Hand off to Codex for continuation", summary)

    def test_status_lists_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            self.start_snapshot_session(tmpdir, agent="claude", task="First task")
            self.start_snapshot_session(tmpdir, agent="codex", task="Second task")

            data = self.run_cli_json("status", "--repo", tmpdir)
            self.assertEqual(data["command"], "status")
            self.assertEqual(len(data["sessions"]), 2)
            agents = {s["agent"] for s in data["sessions"]}
            self.assertEqual(agents, {"claude", "codex"})

    def test_status_empty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data = self.run_cli_json("status", "--repo", tmpdir)
            self.assertEqual(data["sessions"], [])

    def test_launch_execute_runs_command_and_updates_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir).resolve()
            start_data = self.start_snapshot_session(tmpdir, agent="claude", task="Launch the next agent")
            session_id = start_data["session_id"]

            launch_template = (
                f"cd {{repo_root}} && {shlex.quote(sys.executable)} -c "
                "'from pathlib import Path; Path(\"launch-marker.txt\").write_text(\"ok\")' {resume_path}"
            )
            self.run_cli_json(
                "prepare",
                session_id,
                "--snapshot-mode",
                "full",
                "--next-action",
                "Ready to execute handoff",
                "--repo",
                tmpdir,
            )

            self.run_cli_json(
                "failover",
                session_id,
                "--to-agent",
                "codex",
                "--reason",
                "ready to execute handoff",
                "--repo",
                tmpdir,
                extra_env={
                    "AGENT_RELAY_CODEX_LAUNCH_TEMPLATE": launch_template,
                },
            )

            result = self.run_cli(
                "--json",
                "launch",
                session_id,
                "--repo",
                tmpdir,
                "--execute",
            )

            launch_data = json.loads(result.stdout)
            marker_path = repo_root / "launch-marker.txt"

            self.assertEqual(result.returncode, 0)
            self.assertTrue(marker_path.exists())
            self.assertEqual(marker_path.read_text(), "ok")
            self.assertFalse(launch_data["ownership_transferred"])

            awaiting = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            self.assertEqual(awaiting["current_agent"], "claude")
            self.assertEqual(awaiting["current_status"], "awaiting_resume")
            self.assertEqual(awaiting["handoffs"][0]["launch_status"], "succeeded")

            resume = self.run_cli_json("resume", session_id, "--repo", tmpdir)
            self.assertEqual(resume["current_agent"], "codex")
            after_resume = self.run_cli_json("inspect", session_id, "--repo", tmpdir)
            self.assertEqual(after_resume["current_agent"], "codex")
            self.assertEqual(after_resume["current_status"], "active")


class RaceCliHelpersTests(TestCase):
    def test_auto_open_terminals_disabled_when_not_interactive(self) -> None:
        self.assertFalse(_should_auto_open_terminals(interactive=False, requested=True))

    def test_auto_open_terminals_honors_explicit_request(self) -> None:
        self.assertTrue(_should_auto_open_terminals(interactive=True, requested=True))
        self.assertFalse(_should_auto_open_terminals(interactive=True, requested=False))

    @patch.dict("os.environ", {"AGENT_RELAY_OPEN_TERMINALS": "1"}, clear=False)
    def test_auto_open_terminals_honors_env_override(self) -> None:
        self.assertTrue(_should_auto_open_terminals(interactive=True, requested=None))

    @patch("sys.platform", "darwin")
    @patch.dict("os.environ", {}, clear=True)
    def test_auto_open_terminals_defaults_on_for_macos(self) -> None:
        self.assertTrue(_should_auto_open_terminals(interactive=True, requested=None))

    @patch("sys.platform", "linux")
    def test_open_tmux_session_in_terminal_reports_unsupported_platform(self) -> None:
        self.assertEqual(
            _open_tmux_session_in_terminal("relay-test-00"),
            "Automatic terminal opening is currently only supported on macOS.",
        )

    @patch("sys.platform", "darwin")
    @patch.dict("os.environ", {"TERM_PROGRAM": "iTerm.app"}, clear=False)
    @patch("subprocess.run")
    def test_open_tmux_session_in_terminal_uses_osascript_for_iterm(self, run_mock) -> None:
        run_mock.return_value = subprocess.CompletedProcess(args=["osascript"], returncode=0, stdout="", stderr="")
        error = _open_tmux_session_in_terminal("relay-test-00")
        self.assertIsNone(error)
        self.assertEqual(run_mock.call_args.args[0][:2], ["osascript", "-e"])
        self.assertIn('tell application "iTerm"', run_mock.call_args.args[0][2])

    def test_race_result_metadata_surfaces_conflict_paths_and_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "conflicts.json"
            artifact_path.write_text(json.dumps({
                "status": "manual_resolution_required",
                "paths": [
                    {"path": "README.md"},
                    {"path": "docs/guide.md"},
                ],
            }), encoding="utf-8")
            result = ConcurrentResult(
                session_id="sess-123",
                agents=("claude", "codex"),
                tmux_sessions=("relay-sess-123-00", "relay-sess-123-01"),
                continued_from_session_id=None,
                claim_ledger_path=None,
                stop_reason="manual_resolution_required",
                elapsed_seconds=12.0,
                outcomes=(),
                conflict_artifact_path=str(artifact_path),
            )
            metadata = _race_result_metadata(result)
            self.assertEqual(metadata["conflict_paths"], ["README.md", "docs/guide.md"])
            self.assertEqual(metadata["scope_violation_paths"], [])
            self.assertEqual(metadata["next_action"], "agent-relay resolve sess-123")

    def test_race_result_metadata_surfaces_scope_violation_paths(self) -> None:
        outcome = AgentOutcome(
            slot=0,
            agent_key="claude",
            tmux_session="relay-test-00",
            phase="implementation",
            exit_code=0,
            raw_stdout="",
            raw_stderr="",
            text="",
            summary="",
            done_signal=False,
            started_at="",
            finished_at="",
            scope_violations=("src/unexpected.py",),
        )
        result = ConcurrentResult(
            session_id="sess-456",
            agents=("claude", "codex"),
            tmux_sessions=("relay-sess-456-00", "relay-sess-456-01"),
            continued_from_session_id=None,
            claim_ledger_path=None,
            stop_reason="scope_violation",
            elapsed_seconds=9.0,
            outcomes=(outcome,),
        )
        metadata = _race_result_metadata(result)
        self.assertEqual(metadata["conflict_paths"], [])
        self.assertEqual(metadata["scope_violation_paths"], ["src/unexpected.py"])
        self.assertNotIn("next_action", metadata)

    def test_resolve_command_uses_inferred_session_and_agents(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--json", "resolve", "sess-123", "--repo", str(ROOT)])
        args.console = create_console(json_mode=True, quiet=False)
        result = ConcurrentResult(
            session_id="sess-456",
            agents=("claude", "codex"),
            tmux_sessions=("relay-sess-456-00", "relay-sess-456-01"),
            continued_from_session_id="sess-123",
            claim_ledger_path=None,
            stop_reason="all_done",
            elapsed_seconds=5.0,
            outcomes=(),
        )
        with patch("agent_relay.concurrent.infer_conflict_resolution_context", return_value={
            "session_id": "sess-123",
            "status": "manual_resolution_required",
            "agents": ["claude", "codex"],
            "conflict_artifact_path": "/tmp/conflicts.json",
        }), patch(
            "agent_relay.concurrent.run_concurrent",
            return_value=result,
        ) as run_mock, patch(
            "agent_relay.cli.emit_json",
        ) as emit_json_mock:
            exit_code = args.func(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_mock.call_args.kwargs["agents"], ["claude", "codex"])
        self.assertEqual(run_mock.call_args.kwargs["continue_from_session_id"], "sess-123")
        self.assertEqual(
            run_mock.call_args.kwargs["task"],
            "Resolve the remaining conflict and review the final merged result.",
        )
        payload = emit_json_mock.call_args.args[0]
        self.assertEqual(payload["command"], "resolve")
        self.assertEqual(payload["source_session_id"], "sess-123")

    def test_resolve_command_defaults_to_latest_unresolved_session(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["--json", "resolve", "--repo", str(ROOT)])
        args.console = create_console(json_mode=True, quiet=False)
        result = ConcurrentResult(
            session_id="sess-999",
            agents=("claude", "codex"),
            tmux_sessions=("relay-sess-999-00", "relay-sess-999-01"),
            continued_from_session_id="sess-latest",
            claim_ledger_path=None,
            stop_reason="all_done",
            elapsed_seconds=4.0,
            outcomes=(),
        )
        with patch(
            "agent_relay.concurrent.latest_unresolved_conflict_session_id",
            return_value="sess-latest",
        ), patch(
            "agent_relay.concurrent.infer_conflict_resolution_context",
            return_value={
                "session_id": "sess-latest",
                "status": "merge_conflict",
                "agents": ["claude", "codex"],
                "conflict_artifact_path": "/tmp/conflicts.json",
            },
        ), patch(
            "agent_relay.concurrent.run_concurrent",
            return_value=result,
        ) as run_mock, patch(
            "agent_relay.cli.emit_json",
        ):
            exit_code = args.func(args)

        self.assertEqual(exit_code, 0)
        self.assertEqual(run_mock.call_args.kwargs["continue_from_session_id"], "sess-latest")

    def test_parser_help_describes_race_resolution_commands(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        self.assertIn("race", help_text)
        self.assertIn("resolve", help_text)
        self.assertIn("inspect-conflicts", help_text)
        self.assertIn("Concurrent workflow with planning, worktrees, and", help_text)
        self.assertIn("conflict recovery", help_text)
