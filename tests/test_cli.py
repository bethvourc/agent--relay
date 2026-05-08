from __future__ import annotations

import json
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
    def run_cli(
        self, *args: str, extra_env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
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

    def test_status_empty_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            data = self.run_cli_json("status", "--repo", tmpdir)
            self.assertEqual(data["sessions"], [])


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
        run_mock.return_value = subprocess.CompletedProcess(
            args=["osascript"], returncode=0, stdout="", stderr=""
        )
        error = _open_tmux_session_in_terminal("relay-test-00")
        self.assertIsNone(error)
        self.assertEqual(run_mock.call_args.args[0][:2], ["osascript", "-e"])
        self.assertIn('tell application "iTerm"', run_mock.call_args.args[0][2])

    def test_race_result_metadata_surfaces_conflict_paths_and_next_action(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "conflicts.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "status": "manual_resolution_required",
                        "paths": [
                            {"path": "README.md"},
                            {"path": "docs/guide.md"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
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
        with (
            patch(
                "agent_relay.concurrent.infer_conflict_resolution_context",
                return_value={
                    "session_id": "sess-123",
                    "status": "manual_resolution_required",
                    "agents": ["claude", "codex"],
                    "conflict_artifact_path": "/tmp/conflicts.json",
                },
            ),
            patch(
                "agent_relay.concurrent.run_concurrent",
                return_value=result,
            ) as run_mock,
            patch(
                "agent_relay.cli.emit_json",
            ) as emit_json_mock,
        ):
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
        with (
            patch(
                "agent_relay.concurrent.latest_unresolved_conflict_session_id",
                return_value="sess-latest",
            ),
            patch(
                "agent_relay.concurrent.infer_conflict_resolution_context",
                return_value={
                    "session_id": "sess-latest",
                    "status": "merge_conflict",
                    "agents": ["claude", "codex"],
                    "conflict_artifact_path": "/tmp/conflicts.json",
                },
            ),
            patch(
                "agent_relay.concurrent.run_concurrent",
                return_value=result,
            ) as run_mock,
            patch(
                "agent_relay.cli.emit_json",
            ),
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
