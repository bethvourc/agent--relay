"""Tests for concurrent agent execution."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from agent_relay.concurrent import (
    AgentOutcome,
    ClaimSpec,
    ConcurrentControl,
    ConcurrentResult,
    _build_concurrent_prompt,
    _build_shell_command,
    parse_concurrent_control,
    run_concurrent,
    _require_tmux,
)
from agent_relay.hashing import sha256_path


class BuildConcurrentPromptTests(TestCase):
    def test_includes_task_and_agent_info(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Fix all tests",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/repo/.agent-relay/sessions/abc/workspace-log.md"),
            pane_snapshot_paths=[Path("/tmp/repo/pane-0.txt"), Path("/tmp/repo/pane-1.txt")],
        )
        self.assertIn("Fix all tests", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("CONCURRENT", prompt)
        self.assertIn("workspace-log.md", prompt)
        self.assertIn("slot 0", prompt.lower())
        self.assertIn("There is no interactive approval loop in concurrent mode", prompt)

    def test_lists_other_agents_with_slot_numbers(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Collaborate",
            slot=1,
            agent_key="codex",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[Path("/tmp/pane-0.txt"), Path("/tmp/pane-1.txt")],
        )
        self.assertIn("Slot 0", prompt)
        self.assertIn("Claude Code", prompt)
        self.assertIn("/tmp/pane-0.txt", prompt)
        self.assertNotIn("tmux capture-pane", prompt)

    def test_includes_completion_marker(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Do work",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[Path("/tmp/pane-0.txt"), Path("/tmp/pane-1.txt")],
        )
        self.assertIn("RELAY_STATUS:", prompt)
        self.assertIn("continue, blocked, done, error", prompt)

    def test_planning_prompt_requires_claims(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Plan first",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[Path("/tmp/pane-0.txt"), Path("/tmp/pane-1.txt")],
            phase="planning",
        )
        self.assertIn("## Planning Phase", prompt)
        self.assertIn('"status":"planning"', prompt)
        self.assertIn("claims", prompt)
        self.assertIn("owner = exclusive editor", prompt)
        self.assertIn("reviewer = inspect/review only", prompt)

    def test_snapshot_instructions_per_slot(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Team task",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex", "claude"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[
                Path("/tmp/pane-0.txt"),
                Path("/tmp/pane-1.txt"),
                Path("/tmp/pane-2.txt"),
            ],
        )
        self.assertIn("/tmp/pane-1.txt", prompt)
        self.assertIn("/tmp/pane-2.txt", prompt)

    def test_resolution_prompt_mentions_conflict_artifact(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Resolve the conflicted README",
            slot=0,
            agent_key="claude",
            all_agents=["claude"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[],
            phase="resolution",
            resolution_conflict_artifact_path=Path("/tmp/repo/.agent-relay/concurrent/conflicts.json"),
            resolution_paths=("README.md",),
        )
        self.assertIn("## Resolution Phase", prompt)
        self.assertIn("conflicts.json", prompt)
        self.assertIn("README.md", prompt)
        self.assertIn("baseline version", prompt)

    def test_includes_continuation_context_when_requested(self) -> None:
        prompt = _build_concurrent_prompt(
            task="Follow up on the prior review",
            slot=0,
            agent_key="claude",
            all_agents=["claude", "codex"],
            repo_root=Path("/tmp/repo"),
            workspace_log=Path("/tmp/log.md"),
            pane_snapshot_paths=[Path("/tmp/pane-0.txt"), Path("/tmp/pane-1.txt")],
            continued_from_session_id="20260330-abc123",
            continued_workspace_log=Path("/tmp/repo/.agent-relay/sessions/20260330-abc123/workspace-log.md"),
            continued_session_root=Path("/tmp/repo/.agent-relay/sessions/20260330-abc123"),
        )
        self.assertIn("## Continuation Context", prompt)
        self.assertIn("20260330-abc123", prompt)
        self.assertIn("Do not restart from scratch", prompt)


class BuildShellCommandTests(TestCase):
    def test_claude_command_interactive(self) -> None:
        cmd = _build_shell_command("claude", Path("/tmp/prompt.md"), Path("/tmp/repo"), Path("/tmp/exit-code.txt"))
        self.assertIn("claude", cmd)
        self.assertIn("-p", cmd)
        self.assertIn("--permission-mode dontAsk", cmd)
        self.assertIn("exit-code.txt", cmd)
        # Should NOT have stream-json in tmux mode (interactive)
        self.assertNotIn("stream-json", cmd)

    def test_codex_command_interactive(self) -> None:
        cmd = _build_shell_command("codex", Path("/tmp/prompt.md"), Path("/tmp/repo"), Path("/tmp/exit-code.txt"))
        self.assertIn("codex", cmd)
        self.assertIn("-a never", cmd)
        self.assertIn("-s workspace-write", cmd)
        # Should NOT have --json in tmux mode
        self.assertNotIn("--json", cmd)


class ParseConcurrentControlTests(TestCase):
    def test_parses_structured_status(self) -> None:
        control = parse_concurrent_control(
            'Notes\nRELAY_STATUS: {"status":"done","reason":"Finished","remaining_work":[],"verification":["pytest"]}'
        )
        self.assertEqual(
            control,
            ConcurrentControl(
                status="done",
                reason="Finished",
                remaining_work=(),
                verification=("pytest",),
            ),
        )

    def test_parses_claim_objects_with_roles(self) -> None:
        control = parse_concurrent_control(
            'Plan\nRELAY_STATUS: {"status":"planning","reason":"Split work","claims":[{"path":"README.md","role":"owner"},{"path":"src/agent_relay/","role":"reviewer"}],"remaining_work":["implement"],"verification":[]}'
        )
        self.assertEqual(
            control.claim_specs,
            (
                ClaimSpec(path="README.md", role="owner"),
                ClaimSpec(path="src/agent_relay/", role="reviewer"),
            ),
        )
        self.assertEqual(control.claims, ("README.md", "src/agent_relay/"))

    def test_legacy_marker_requires_standalone_line(self) -> None:
        control = parse_concurrent_control("Notes\nCONVERSATION_COMPLETE")
        self.assertEqual(control.status, "done")

    def test_inline_marker_is_not_a_done_signal(self) -> None:
        control = parse_concurrent_control("I mentioned CONVERSATION_COMPLETE in my notes.")
        self.assertEqual(control.status, "continue")


class RequireTmuxTests(TestCase):
    @patch("shutil.which", return_value="/usr/bin/tmux")
    def test_passes_when_tmux_available(self, mock_which) -> None:
        path = _require_tmux()
        self.assertEqual(path, "/usr/bin/tmux")

    @patch("shutil.which", return_value=None)
    def test_raises_when_tmux_missing(self, mock_which) -> None:
        with self.assertRaises(SystemExit) as ctx:
            _require_tmux()
        self.assertIn("tmux is required", str(ctx.exception))


class AgentOutcomeTests(TestCase):
    def test_dataclass_creation(self) -> None:
        outcome = AgentOutcome(
            slot=0,
            agent_key="claude",
            tmux_session="relay-test-00",
            phase="implementation",
            exit_code=0,
            raw_stdout="output",
            raw_stderr="",
            text="normalized",
            summary="Did stuff.",
            done_signal=True,
            started_at="2025-01-01T00:00:00Z",
            finished_at="2025-01-01T00:01:00Z",
        )
        self.assertEqual(outcome.slot, 0)
        self.assertEqual(outcome.agent_key, "claude")
        self.assertEqual(outcome.tmux_session, "relay-test-00")
        self.assertTrue(outcome.done_signal)

    def test_none_exit_code_for_killed(self) -> None:
        outcome = AgentOutcome(
            slot=0, agent_key="claude", tmux_session="relay-test-00", phase="planning", exit_code=None,
            raw_stdout="", raw_stderr="", text="", summary="",
            done_signal=False, started_at="", finished_at="",
        )
        self.assertIsNone(outcome.exit_code)


class ConcurrentResultTests(TestCase):
    def test_dataclass_creation(self) -> None:
        result = ConcurrentResult(
            session_id="test-123",
            agents=("claude", "codex"),
            tmux_sessions=("relay-test-00", "relay-test-01"),
            continued_from_session_id=None,
            claim_ledger_path=None,
            stop_reason="all_done",
            elapsed_seconds=42.5,
            outcomes=(),
        )
        self.assertEqual(result.session_id, "test-123")
        self.assertEqual(result.stop_reason, "all_done")
        self.assertEqual(result.elapsed_seconds, 42.5)
        self.assertEqual(result.agents, ("claude", "codex"))
        self.assertEqual(result.tmux_sessions, ("relay-test-00", "relay-test-01"))


class RunConcurrentTests(TestCase):
    @staticmethod
    def _phase_and_slot(session_name: str) -> tuple[str, int]:
        slot = int(session_name.rsplit("-", 1)[-1])
        if "-planning-" in session_name:
            phase = "planning"
        elif "-resolution-" in session_name:
            phase = "resolution"
        elif "-review-" in session_name:
            phase = "review"
        else:
            phase = "implementation"
        return phase, slot

    def _run(
        self,
        *,
        planning_contents: dict[int, str],
        implementation_contents: dict[int, str] | None = None,
        resolution_contents: dict[int, str] | None = None,
        review_contents: dict[int, str] | None = None,
        planning_exit_codes: dict[int, int | None] | None = None,
        implementation_exit_codes: dict[int, int | None] | None = None,
        resolution_exit_codes: dict[int, int | None] | None = None,
        review_exit_codes: dict[int, int | None] | None = None,
        baseline_files: dict[str, str] | None = None,
        worktree_overrides: dict[int, dict[str, str | None]] | None = None,
        resolution_worktree_overrides: dict[int, dict[str, str | None]] | None = None,
        continue_from_session_id: str | None = None,
        prior_conflict_artifact: dict[str, object] | None = None,
        prior_conflict_files: dict[str, str | bytes] | None = None,
        pane_dead,
        session_exists,
        max_time_seconds: int = 600,
        on_agent_start=None,
        repo_root: Path | None = None,
    ) -> ConcurrentResult:
        implementation_contents = implementation_contents or {}
        resolution_contents = resolution_contents or {}
        review_contents = review_contents or {}
        planning_exit_codes = planning_exit_codes or {}
        implementation_exit_codes = implementation_exit_codes or {}
        resolution_exit_codes = resolution_exit_codes or {}
        review_exit_codes = review_exit_codes or {}
        baseline_files = baseline_files or {
            "README.md": "baseline readme\n",
            "tests/test_concurrent.py": "baseline tests\n",
        }
        worktree_overrides = worktree_overrides or {}
        resolution_worktree_overrides = resolution_worktree_overrides or {}
        prior_conflict_files = prior_conflict_files or {}
        def run_in_repo(repo_root: Path) -> ConcurrentResult:
            for relative_path, content in baseline_files.items():
                target = repo_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            if continue_from_session_id and prior_conflict_artifact is not None:
                artifact_dir = repo_root / ".agent-relay" / "sessions" / continue_from_session_id / "concurrent"
                (artifact_dir / "conflicts").mkdir(parents=True, exist_ok=True)
                for relative_path in baseline_files:
                    source = repo_root / relative_path
                    if source.exists():
                        destination = artifact_dir / "conflicts" / "repo" / relative_path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                for relative_path, content in prior_conflict_files.items():
                    destination = artifact_dir / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(content, bytes):
                        destination.write_bytes(content)
                    else:
                        destination.write_text(content, encoding="utf-8")
                (artifact_dir / "conflicts.json").write_text(
                    json.dumps(prior_conflict_artifact),
                    encoding="utf-8",
                )

            worktree_paths: dict[int, Path] = {}

            def fake_create_worktree_at(
                worktree_path: Path,
                slot: int,
                baseline_paths,
                overrides: dict[int, dict[str, str | None]],
            ) -> Path:
                for relative_path in baseline_paths:
                    source = repo_root / relative_path
                    destination = worktree_path / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                for relative_path, content in overrides.get(slot, {}).items():
                    destination = worktree_path / relative_path
                    if content is None:
                        if destination.exists():
                            destination.unlink()
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if isinstance(content, bytes):
                        destination.write_bytes(content)
                    else:
                        destination.write_text(content, encoding="utf-8")
                worktree_paths[slot] = worktree_path
                return worktree_path

            def fake_create_worktree(
                _repo_root: Path,
                *,
                session_id: str,
                slot: int,
                baseline_paths,
            ) -> Path:
                worktree_path = repo_root / f"worktree-{session_id}-{slot:02d}"
                return fake_create_worktree_at(worktree_path, slot, baseline_paths, worktree_overrides)

            def fake_create_resolution_worktree(
                _repo_root: Path,
                *,
                session_id: str,
                slot: int,
                baseline_paths,
            ) -> Path:
                worktree_path = repo_root / f"resolver-{session_id}-{slot:02d}"
                return fake_create_worktree_at(worktree_path, slot, baseline_paths, resolution_worktree_overrides)

            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session"
            ), patch(
                "agent_relay.concurrent.is_session",
                return_value=True if continue_from_session_id else False,
            ), patch(
                "agent_relay.concurrent._current_repo_file_paths",
                return_value=tuple(sorted(baseline_files)),
            ), patch(
                "agent_relay.concurrent._build_baseline_manifest",
                side_effect=lambda _repo_root, _relative_paths: {
                    relative_path: sha256_path(repo_root / relative_path)
                    for relative_path in baseline_files
                },
            ), patch(
                "agent_relay.concurrent._create_agent_worktree",
                side_effect=fake_create_worktree,
            ), patch(
                "agent_relay.concurrent._create_resolution_worktree",
                side_effect=fake_create_resolution_worktree,
            ), patch(
                "agent_relay.concurrent._cleanup_worktrees",
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                side_effect=session_exists,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                side_effect=pane_dead,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda session_name, _slot: (
                    planning_contents.get(self._phase_and_slot(session_name)[1], "")
                    if self._phase_and_slot(session_name)[0] == "planning"
                    else resolution_contents.get(self._phase_and_slot(session_name)[1], "")
                    if self._phase_and_slot(session_name)[0] == "resolution"
                    else review_contents.get(self._phase_and_slot(session_name)[1], "")
                    if self._phase_and_slot(session_name)[0] == "review"
                    else implementation_contents.get(self._phase_and_slot(session_name)[1], "")
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                side_effect=lambda path: (
                    planning_exit_codes.get(int(path.parent.name.split("-")[-1]), None)
                    if path.name == "planning-exit-code.txt"
                    else resolution_exit_codes.get(int(path.parent.name.split("-")[-1]), None)
                    if path.name == "resolution-exit-code.txt"
                    else review_exit_codes.get(int(path.parent.name.split("-")[-1]), None)
                    if path.name == "review-exit-code.txt"
                    else implementation_exit_codes.get(int(path.parent.name.split("-")[-1]), None)
                ),
            ):
                return run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Finish the task",
                    continue_from_session_id=continue_from_session_id,
                    max_time_seconds=max_time_seconds,
                    on_agent_start=on_agent_start,
                )

        if repo_root is not None:
            return run_in_repo(repo_root)

        with TemporaryDirectory() as tmpdir:
            return run_in_repo(Path(tmpdir))

    def test_all_done_requires_done_status_from_all_panes(self) -> None:
        result = self._run(
            planning_contents={
                0: 'Planning done\nRELAY_STATUS: {"status":"planning","reason":"Own docs","claims":["README.md"],"remaining_work":["implement docs"],"verification":[]}',
                1: 'Planning done\nRELAY_STATUS: {"status":"planning","reason":"Own tests","claims":["tests/test_concurrent.py"],"remaining_work":["implement tests"],"verification":[]}',
            },
            implementation_contents={
                0: 'Agent 0 finished\nRELAY_STATUS: {"status":"done","reason":"Complete","remaining_work":[],"verification":["review"]}',
                1: 'Agent 1 finished\nRELAY_STATUS: {"status":"done","reason":"Complete","remaining_work":[],"verification":["tests"]}',
            },
            planning_exit_codes={0: 0, 1: 0},
            implementation_exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "all_done")
        self.assertTrue(all(outcome.done_signal for outcome in result.outcomes))
        self.assertEqual([outcome.control_status for outcome in result.outcomes], ["done", "done"])
        self.assertTrue(all(outcome.phase == "implementation" for outcome in result.outcomes))
        self.assertEqual(
            [outcome.tmux_session for outcome in result.outcomes],
            [f"relay-{result.session_id}-00", f"relay-{result.session_id}-01"],
        )

    def test_start_session_uses_schema_valid_workstream_kind_and_separate_tmux_sessions(self) -> None:
        start_session_mock = MagicMock()
        tmux_mock = MagicMock(return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""))
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "README.md").write_text("baseline\n", encoding="utf-8")
            baseline_files = ("README.md",)

            def fake_create_worktree(
                _repo_root: Path,
                *,
                session_id: str,
                slot: int,
                baseline_paths,
            ) -> Path:
                worktree_path = repo_root / f"worktree-{session_id}-{slot:02d}"
                for relative_path in baseline_paths:
                    source = repo_root / relative_path
                    destination = worktree_path / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                return worktree_path

            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session",
                start_session_mock,
            ), patch(
                "agent_relay.concurrent._current_repo_file_paths",
                return_value=baseline_files,
            ), patch(
                "agent_relay.concurrent._build_baseline_manifest",
                return_value={"README.md": sha256_path(repo_root / "README.md")},
            ), patch(
                "agent_relay.concurrent._create_agent_worktree",
                side_effect=fake_create_worktree,
            ), patch(
                "agent_relay.concurrent._tmux",
                tmux_mock,
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda session_name, _slot: (
                    'Plan\nRELAY_STATUS: {"status":"planning","reason":"Ready","claims":["README.md"],"remaining_work":["implement"],"verification":[]}'
                    if "-planning-" in session_name and session_name.endswith("-00")
                    else 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Ready","claims":["tests/test_concurrent.py"],"remaining_work":["implement"],"verification":[]}'
                    if "-planning-" in session_name
                    else 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":[]}'
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=0,
            ):
                run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Finish the task",
                )

        self.assertEqual(start_session_mock.call_args.kwargs["workstream_kind"], "mixed")
        session_id = start_session_mock.call_args.kwargs["session_id"]
        new_session_calls = [
            call.args
            for call in tmux_mock.call_args_list
            if call.args and call.args[0] == "new-session"
        ]
        self.assertEqual(len(new_session_calls), 4)
        self.assertIn(
            ("set-option", "-t", f"relay-{session_id}-planning-00", "mouse", "on"),
            [call.args[:5] for call in tmux_mock.call_args_list if len(call.args) >= 5],
        )
        self.assertIn(
            ("set-option", "-t", f"relay-{session_id}-planning-01", "mouse", "on"),
            [call.args[:5] for call in tmux_mock.call_args_list if len(call.args) >= 5],
        )
        self.assertIn(
            ("set-option", "-t", f"relay-{session_id}-00", "mouse", "on"),
            [call.args[:5] for call in tmux_mock.call_args_list if len(call.args) >= 5],
        )
        self.assertIn(
            ("set-option", "-t", f"relay-{session_id}-01", "mouse", "on"),
            [call.args[:5] for call in tmux_mock.call_args_list if len(call.args) >= 5],
        )

    def test_clean_exit_without_done_is_incomplete(self) -> None:
        result = self._run(
            planning_contents={
                0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}',
                1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}',
            },
            implementation_contents={
                0: 'Still work left\nRELAY_STATUS: {"status":"continue","reason":"Docs pending","remaining_work":["docs"],"verification":[]}',
                1: 'Finished my part\nRELAY_STATUS: {"status":"done","reason":"Code ready","remaining_work":[],"verification":["tests"]}',
            },
            planning_exit_codes={0: 0, 1: 0},
            implementation_exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "incomplete")
        self.assertEqual([outcome.control_status for outcome in result.outcomes], ["continue", "done"])

    def test_nonzero_exit_is_agent_error(self) -> None:
        result = self._run(
            planning_contents={
                0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Code","claims":["src/agent_relay/concurrent.py"],"remaining_work":["code"],"verification":[]}',
                1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Review","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}',
            },
            implementation_contents={
                0: 'Build failed\nRELAY_STATUS: {"status":"error","reason":"Tests failed","remaining_work":["fix tests"],"verification":["pytest failed"]}',
                1: 'Finished my part\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":["review"]}',
            },
            planning_exit_codes={0: 0, 1: 0},
            implementation_exit_codes={0: 1, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "agent_error")
        self.assertEqual(result.outcomes[0].exit_code, 1)
        self.assertEqual(result.outcomes[0].control_status, "error")

    def test_in_scope_changes_merge_back_to_main_repo(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={
                    0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}',
                    1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}',
                },
                implementation_contents={
                    0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Updated docs","remaining_work":[],"verification":["review"]}',
                    1: 'Done\nRELAY_STATUS: {"status":"done","reason":"No changes","remaining_work":[],"verification":["review"]}',
                },
                baseline_files={
                    "README.md": "before\n",
                    "tests/test_concurrent.py": "baseline tests\n",
                },
                worktree_overrides={
                    0: {"README.md": "after\n"},
                },
                planning_exit_codes={0: 0, 1: 0},
                implementation_exit_codes={0: 0, 1: 0},
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "all_done")
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "after\n")
            self.assertEqual(result.outcomes[0].merged_paths, ("README.md",))
            self.assertTrue(result.outcomes[0].worktree_path is not None)

    def test_out_of_scope_changes_block_merge(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={
                    0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}',
                    1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}',
                },
                implementation_contents={
                    0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Changed docs","remaining_work":[],"verification":["review"]}',
                    1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":["review"]}',
                },
                worktree_overrides={
                    0: {
                        "README.md": "claimed change\n",
                        "src/unexpected.py": "print('oops')\n",
                    },
                },
                planning_exit_codes={0: 0, 1: 0},
                implementation_exit_codes={0: 0, 1: 0},
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "scope_violation")
            self.assertIn("src/unexpected.py", result.outcomes[0].scope_violations)
            self.assertEqual(result.outcomes[0].merged_paths, ())
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "baseline readme\n")
            self.assertFalse((repo_root / "src" / "unexpected.py").exists())

    def test_shared_claims_can_collaboratively_merge_same_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={
                    0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Shared docs","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                    1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Shared docs too","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                },
                implementation_contents={
                    0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Updated intro","remaining_work":[],"verification":["review"]}',
                    1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Updated outro","remaining_work":[],"verification":["review"]}',
                },
                baseline_files={
                    "README.md": "alpha\nbeta\ngamma\n",
                    "tests/test_concurrent.py": "baseline tests\n",
                },
                worktree_overrides={
                    0: {"README.md": "alpha from slot0\nbeta\ngamma\n"},
                    1: {"README.md": "alpha\nbeta\ngamma from slot1\n"},
                },
                planning_exit_codes={0: 0, 1: 0},
                implementation_exit_codes={0: 0, 1: 0},
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "all_done")
            self.assertEqual(
                (repo_root / "README.md").read_text(encoding="utf-8"),
                "alpha from slot0\nbeta\ngamma from slot1\n",
            )
            self.assertEqual(result.outcomes[1].merged_paths, ("README.md",))

    def test_merge_conflict_runs_resolution_phase_and_resolves_file(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={
                    0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Shared docs","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                    1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Shared docs too","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                },
                implementation_contents={
                    0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Intro change","remaining_work":[],"verification":["review"]}',
                    1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Conflicting change","remaining_work":[],"verification":["review"]}',
                },
                resolution_contents={
                    0: 'Resolved\nRELAY_STATUS: {"status":"done","reason":"Chose final README","remaining_work":[],"verification":["manual review"]}',
                },
                review_contents={
                    0: 'Reviewed\nRELAY_STATUS: {"status":"done","reason":"Resolution looks correct","remaining_work":[],"verification":["manual review"]}',
                },
                baseline_files={
                    "README.md": "shared line\n",
                    "tests/test_concurrent.py": "baseline tests\n",
                },
                worktree_overrides={
                    0: {"README.md": "slot zero line\n"},
                    1: {"README.md": "slot one line\n"},
                },
                resolution_worktree_overrides={
                    0: {"README.md": "resolved line\n"},
                },
                planning_exit_codes={0: 0, 1: 0},
                implementation_exit_codes={0: 0, 1: 0},
                resolution_exit_codes={0: 0},
                review_exit_codes={1: 0},
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "all_done")
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "resolved line\n")
            self.assertIsNotNone(result.conflict_artifact_path)
            self.assertTrue(Path(result.conflict_artifact_path).exists())
            self.assertTrue(all(not outcome.merge_conflicts for outcome in result.outcomes))

    def test_failed_resolution_preserves_merge_conflict_and_artifact(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={
                    0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Shared docs","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                    1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Shared docs too","claims":[{"path":"README.md","role":"shared"}],"remaining_work":["docs"],"verification":[]}',
                },
                implementation_contents={
                    0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Intro change","remaining_work":[],"verification":["review"]}',
                    1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Conflicting change","remaining_work":[],"verification":["review"]}',
                },
                resolution_contents={
                    0: 'Blocked\nRELAY_STATUS: {"status":"blocked","reason":"Need human decision","remaining_work":["manual choice"],"verification":[]}',
                },
                baseline_files={
                    "README.md": "shared line\n",
                    "tests/test_concurrent.py": "baseline tests\n",
                },
                worktree_overrides={
                    0: {"README.md": "slot zero line\n"},
                    1: {"README.md": "slot one line\n"},
                },
                planning_exit_codes={0: 0, 1: 0},
                implementation_exit_codes={0: 0, 1: 0},
                resolution_exit_codes={0: 0},
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "manual_resolution_required")
            self.assertIsNotNone(result.conflict_artifact_path)
            artifact = Path(result.conflict_artifact_path)
            self.assertTrue(artifact.exists())
            self.assertIn('"status": "manual_resolution_required"', artifact.read_text(encoding="utf-8"))
            self.assertTrue(any(outcome.merge_conflicts for outcome in result.outcomes))

    def test_continue_from_conflict_artifact_starts_resolution_and_review(self) -> None:
        prior_artifact = {
            "session_id": "prior-session",
            "status": "manual_resolution_required",
            "paths": [
                {
                    "path": "README.md",
                    "base_version": {"exists": True, "path": "conflicts/base/README.md"},
                    "repo_version": {"exists": True, "path": "conflicts/repo/README.md"},
                    "contributors": [
                        {"slot": 0, "agent": "claude", "version_path": "conflicts/slot-00/README.md"},
                        {"slot": 1, "agent": "codex", "version_path": "conflicts/slot-01/README.md"},
                    ],
                }
            ],
        }
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={},
                implementation_contents={},
                resolution_contents={
                    0: 'Resolved\nRELAY_STATUS: {"status":"done","reason":"Chose final README","remaining_work":[],"verification":["manual review"]}',
                },
                review_contents={
                    0: 'Reviewed\nRELAY_STATUS: {"status":"done","reason":"Resolution looks correct","remaining_work":[],"verification":["manual review"]}',
                },
                baseline_files={
                    "README.md": "current repo draft\n",
                    "tests/test_concurrent.py": "baseline tests\n",
                },
                resolution_worktree_overrides={
                    0: {"README.md": "resolved from continuation\n"},
                },
                resolution_exit_codes={0: 0},
                review_exit_codes={1: 0},
                continue_from_session_id="prior-session",
                prior_conflict_artifact=prior_artifact,
                prior_conflict_files={
                    "conflicts/base/README.md": "base version\n",
                    "conflicts/slot-00/README.md": "slot zero version\n",
                    "conflicts/slot-01/README.md": "slot one version\n",
                },
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "all_done")
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "resolved from continuation\n")
            self.assertEqual([outcome.phase for outcome in result.outcomes], ["resolution", "review"])
            self.assertTrue(all(outcome.control_status == "done" for outcome in result.outcomes))

    def test_continue_from_binary_conflict_artifact_requires_manual_resolution(self) -> None:
        prior_artifact = {
            "session_id": "prior-session",
            "status": "merge_conflict",
            "paths": [
                {
                    "path": "assets/logo.bin",
                    "base_version": {"exists": True, "path": "conflicts/base/assets/logo.bin"},
                    "repo_version": {"exists": True, "path": "conflicts/repo/assets/logo.bin"},
                    "contributors": [
                        {"slot": 0, "agent": "claude", "version_path": "conflicts/slot-00/assets/logo.bin"},
                    ],
                }
            ],
        }
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={},
                implementation_contents={},
                baseline_files={
                    "assets/logo.bin": "text placeholder\n",
                    "tests/test_concurrent.py": "baseline tests\n",
                },
                continue_from_session_id="prior-session",
                prior_conflict_artifact=prior_artifact,
                prior_conflict_files={
                    "conflicts/base/assets/logo.bin": b"\x00\x01base",
                    "conflicts/slot-00/assets/logo.bin": b"\x00\x02slot",
                },
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "manual_resolution_required")
            self.assertEqual(result.outcomes, ())
            self.assertIsNotNone(result.conflict_artifact_path)
            artifact = Path(result.conflict_artifact_path)
            self.assertIn('"manual_paths": [', artifact.read_text(encoding="utf-8"))

    def test_owner_and_reviewer_claims_can_overlap_but_reviewer_cannot_edit(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            result = self._run(
                planning_contents={
                    0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Own docs","claims":[{"path":"README.md","role":"owner"}],"remaining_work":["docs"],"verification":[]}',
                    1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Review docs","claims":[{"path":"README.md","role":"reviewer"}],"remaining_work":["review"],"verification":[]}',
                },
                implementation_contents={
                    0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Updated docs","remaining_work":[],"verification":["review"]}',
                    1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Reviewed","remaining_work":[],"verification":["review"]}',
                },
                worktree_overrides={
                    0: {"README.md": "claimed change\n"},
                    1: {"README.md": "reviewer change\n"},
                },
                planning_exit_codes={0: 0, 1: 0},
                implementation_exit_codes={0: 0, 1: 0},
                pane_dead=lambda _session, _slot: True,
                session_exists=lambda _session: True,
                repo_root=repo_root,
            )
            self.assertEqual(result.stop_reason, "scope_violation")
            self.assertIn("README.md", result.outcomes[1].scope_violations)
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "claimed change\n")

    def test_killed_tmux_session_is_interrupted(self) -> None:
        result = self._run(
            planning_contents={},
            pane_dead=lambda _session, _slot: False,
            session_exists=lambda _session: False,
        )
        self.assertEqual(result.stop_reason, "interrupted")
        self.assertTrue(all(outcome.exit_code is None for outcome in result.outcomes))
        self.assertTrue(all(outcome.phase == "planning" for outcome in result.outcomes))

    def test_timeout_is_enforced_without_attached_tmux_client(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "README.md").write_text("baseline\n", encoding="utf-8")

            def fake_create_worktree(
                _repo_root: Path,
                *,
                session_id: str,
                slot: int,
                baseline_paths,
            ) -> Path:
                worktree_path = repo_root / f"worktree-{session_id}-{slot:02d}"
                for relative_path in baseline_paths:
                    source = repo_root / relative_path
                    destination = worktree_path / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                return worktree_path

            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session"
            ), patch(
                "agent_relay.concurrent._current_repo_file_paths",
                return_value=("README.md",),
            ), patch(
                "agent_relay.concurrent._build_baseline_manifest",
                return_value={"README.md": sha256_path(repo_root / "README.md")},
            ), patch(
                "agent_relay.concurrent._create_agent_worktree",
                side_effect=fake_create_worktree,
            ), patch(
                "agent_relay.concurrent._cleanup_worktrees",
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                return_value=False,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda session_name, _slot: (
                    'Still running\nRELAY_STATUS: {"status":"planning","reason":"Work in progress","claims":["README.md"],"remaining_work":["finish"],"verification":[]}'
                    if int(session_name.rsplit("-", 1)[-1]) == 0
                    else ""
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=None,
            ), patch(
                "time.time",
                return_value=10**20,
            ):
                result = run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_time_seconds=1,
                )
        self.assertEqual(result.stop_reason, "max_time")

    def test_timeout_partially_merges_in_scope_changes(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            baseline_files = {
                "README.md": "baseline readme\n",
                "tests/test_concurrent.py": "baseline tests\n",
            }
            for relative_path, content in baseline_files.items():
                target = repo_root / relative_path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")

            def fake_create_worktree(
                _repo_root: Path,
                *,
                session_id: str,
                slot: int,
                baseline_paths,
            ) -> Path:
                worktree_path = repo_root / f"worktree-{session_id}-{slot:02d}"
                for relative_path in baseline_paths:
                    source = repo_root / relative_path
                    destination = worktree_path / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                if slot == 0:
                    (worktree_path / "README.md").write_text("timed out draft\n", encoding="utf-8")
                return worktree_path

            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session"
            ), patch(
                "agent_relay.concurrent._current_repo_file_paths",
                return_value=tuple(sorted(baseline_files)),
            ), patch(
                "agent_relay.concurrent._build_baseline_manifest",
                side_effect=lambda _repo_root, _relative_paths: {
                    relative_path: sha256_path(repo_root / relative_path)
                    for relative_path in baseline_files
                },
            ), patch(
                "agent_relay.concurrent._create_agent_worktree",
                side_effect=fake_create_worktree,
            ), patch(
                "agent_relay.concurrent._cleanup_worktrees",
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                side_effect=lambda session_name, _slot: "-planning-" in session_name,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda session_name, _slot: (
                    (
                        'Plan\nRELAY_STATUS: {"status":"planning","reason":"Docs","claims":[{"path":"README.md","role":"owner"}],"remaining_work":["docs"],"verification":[]}'
                        if session_name.endswith("-00")
                        else 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":[{"path":"tests/test_concurrent.py","role":"owner"}],"remaining_work":["tests"],"verification":[]}'
                    )
                    if "-planning-" in session_name
                    else 'Still running\nRELAY_STATUS: {"status":"continue","reason":"Need more time","remaining_work":["polish"],"verification":[]}'
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                side_effect=lambda path: 0 if path.name == "planning-exit-code.txt" else None,
            ), patch(
                "time.time",
                side_effect=[0, 0, 10**20, 10**20, 10**20],
            ):
                result = run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Finish the task",
                    max_time_seconds=1,
                )
            self.assertEqual(result.stop_reason, "max_time")
            self.assertEqual(result.outcomes[0].merged_paths, ("README.md",))
            self.assertEqual((repo_root / "README.md").read_text(encoding="utf-8"), "timed out draft\n")

    def test_inline_done_marker_text_does_not_count(self) -> None:
        result = self._run(
            planning_contents={
                0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}',
                1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}',
            },
            implementation_contents={
                0: 'I documented the string CONVERSATION_COMPLETE for later.\nRELAY_STATUS: {"status":"continue","reason":"Not finished","remaining_work":["more work"],"verification":[]}',
                1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":["review"]}',
            },
            planning_exit_codes={0: 0, 1: 0},
            implementation_exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "incomplete")
        self.assertEqual(result.outcomes[0].control_status, "continue")

    def test_pane_snapshots_are_written_locally(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "README.md").write_text("baseline\n", encoding="utf-8")

            def fake_create_worktree(
                _repo_root: Path,
                *,
                session_id: str,
                slot: int,
                baseline_paths,
            ) -> Path:
                worktree_path = repo_root / f"worktree-{session_id}-{slot:02d}"
                for relative_path in baseline_paths:
                    source = repo_root / relative_path
                    destination = worktree_path / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                return worktree_path

            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session"
            ), patch(
                "agent_relay.concurrent._current_repo_file_paths",
                return_value=("README.md",),
            ), patch(
                "agent_relay.concurrent._build_baseline_manifest",
                return_value={"README.md": sha256_path(repo_root / "README.md")},
            ), patch(
                "agent_relay.concurrent._create_agent_worktree",
                side_effect=fake_create_worktree,
            ), patch(
                "agent_relay.concurrent._cleanup_worktrees",
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda session_name, _slot: (
                    (
                        'Plan\nRELAY_STATUS: {"status":"planning","reason":"Docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}'
                        if session_name.endswith("-00")
                        else 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}'
                    )
                    if "-planning-" in session_name
                    else f"implementation slot {int(session_name.rsplit('-', 1)[-1])} snapshot"
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=0,
            ):
                result = run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Finish the task",
                )

                session_dir = repo_root / ".agent-relay" / "sessions" / result.session_id / "concurrent"
                self.assertEqual(
                    (session_dir / "agent-00" / "pane.txt").read_text(encoding="utf-8"),
                    "implementation slot 0 snapshot",
                )
                self.assertEqual(
                    (session_dir / "agent-01" / "pane.txt").read_text(encoding="utf-8"),
                    "implementation slot 1 snapshot",
                )
                self.assertEqual(
                    (repo_root / f"worktree-{result.session_id}-00" / ".agent-relay" / "concurrent" / "slot-01.txt").read_text(encoding="utf-8"),
                    "implementation slot 1 snapshot",
                )

    def test_on_agent_start_receives_attachable_tmux_session(self) -> None:
        started: list[tuple[int, str, str]] = []
        result = self._run(
            planning_contents={
                0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}',
                1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}',
            },
            implementation_contents={
                0: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":[]}',
                1: 'Done\nRELAY_STATUS: {"status":"done","reason":"Ready","remaining_work":[],"verification":[]}',
            },
            planning_exit_codes={0: 0, 1: 0},
            implementation_exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
            on_agent_start=lambda slot, agent_key, tmux_session: started.append((slot, agent_key, tmux_session)),
        )
        self.assertEqual(
            started,
            [
                (0, "claude", f"relay-{result.session_id}-00"),
                (1, "codex", f"relay-{result.session_id}-01"),
            ],
        )

    def test_conflicting_planning_claims_block_implementation(self) -> None:
        with TemporaryDirectory() as tmpdir:
            repo_root = Path(tmpdir)
            (repo_root / "README.md").write_text("baseline\n", encoding="utf-8")

            def fake_create_worktree(
                _repo_root: Path,
                *,
                session_id: str,
                slot: int,
                baseline_paths,
            ) -> Path:
                worktree_path = repo_root / f"worktree-{session_id}-{slot:02d}"
                for relative_path in baseline_paths:
                    source = repo_root / relative_path
                    destination = worktree_path / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                return worktree_path

            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ), patch(
                "agent_relay.concurrent.start_session"
            ), patch(
                "agent_relay.concurrent._current_repo_file_paths",
                return_value=("README.md",),
            ), patch(
                "agent_relay.concurrent._build_baseline_manifest",
                return_value={"README.md": sha256_path(repo_root / "README.md")},
            ), patch(
                "agent_relay.concurrent._create_agent_worktree",
                side_effect=fake_create_worktree,
            ), patch(
                "agent_relay.concurrent._cleanup_worktrees",
            ), patch(
                "agent_relay.concurrent._tmux",
                return_value=subprocess.CompletedProcess(args=["tmux"], returncode=0, stdout="", stderr=""),
            ), patch(
                "agent_relay.concurrent._tmux_session_exists",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_pane_dead",
                return_value=True,
            ), patch(
                "agent_relay.concurrent._tmux_capture_pane",
                side_effect=lambda session_name, _slot: (
                    'Plan\nRELAY_STATUS: {"status":"planning","reason":"Own docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}'
                    if session_name.endswith("-00")
                    else 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Also own docs","claims":["README.md"],"remaining_work":["docs"],"verification":[]}'
                ),
            ), patch(
                "agent_relay.concurrent._read_exit_code",
                return_value=0,
            ):
                result = run_concurrent(
                    repo_root,
                    agents=["claude", "codex"],
                    task="Finish the task",
                )
                self.assertEqual(result.stop_reason, "claim_conflict")
                self.assertTrue(all(outcome.phase == "planning" for outcome in result.outcomes))
                ledger = Path(result.claim_ledger_path)
                self.assertTrue(ledger.exists())
                self.assertIn('"status": "claim_conflict"', ledger.read_text(encoding="utf-8"))

    def test_overlapping_owner_and_shared_claims_conflict(self) -> None:
        result = self._run(
            planning_contents={
                0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Own src","claims":[{"path":"src/","role":"owner"}],"remaining_work":["src"],"verification":[]}',
                1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Shared file","claims":[{"path":"src/agent_relay/concurrent.py","role":"shared"}],"remaining_work":["src"],"verification":[]}',
            },
            planning_exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "claim_conflict")

    def test_planning_requires_nonempty_claims(self) -> None:
        result = self._run(
            planning_contents={
                0: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Unsure","claims":[],"remaining_work":["docs"],"verification":[]}',
                1: 'Plan\nRELAY_STATUS: {"status":"planning","reason":"Tests","claims":["tests/test_concurrent.py"],"remaining_work":["tests"],"verification":[]}',
            },
            planning_exit_codes={0: 0, 1: 0},
            pane_dead=lambda _session, _slot: True,
            session_exists=lambda _session: True,
        )
        self.assertEqual(result.stop_reason, "planning_incomplete")

    def test_continue_from_missing_session_raises(self) -> None:
        with TemporaryDirectory() as tmpdir:
            with patch("agent_relay.concurrent._require_tmux"), patch(
                "agent_relay.concurrent.require_available"
            ):
                with self.assertRaises(SystemExit) as ctx:
                    run_concurrent(
                        Path(tmpdir),
                        agents=["claude", "codex"],
                        task="Continue missing work",
                        continue_from_session_id="missing-session",
                    )
        self.assertIn("Session not found: missing-session", str(ctx.exception))
