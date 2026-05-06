from __future__ import annotations

import json
import tempfile
from io import StringIO
from pathlib import Path
from unittest import TestCase

from rich.console import Console

from agent_relay import __version__
from agent_relay.concurrent import AgentOutcome, ConcurrentResult
from agent_relay.ui import (
    RELAY_THEME,
    create_console,
    emit_json,
    emit_quiet,
    is_compact,
    render_banner,
    render_checkpoint_success,
    render_conflict_inspect,
    render_dashboard,
    render_concurrent_result,
    render_error,
    render_failover_success,
    render_help,
    render_inspect,
    render_launch_preview,
    render_launch_result,
    render_pause_success,
    render_prepare_success,
    render_start_success,
    status_badge,
    agent_badge,
)


def make_console(width: int = 120) -> tuple[Console, StringIO]:
    buf = StringIO()
    console = Console(theme=RELAY_THEME, file=buf, width=width, force_terminal=True)
    return console, buf


class ConsoleFactoryTests(TestCase):
    def test_normal_mode_is_not_quiet(self) -> None:
        console = create_console(json_mode=False, quiet=False)
        self.assertFalse(console.quiet)

    def test_json_mode_creates_quiet_console(self) -> None:
        console = create_console(json_mode=True, quiet=False)
        self.assertTrue(console.quiet)

    def test_quiet_mode_creates_quiet_console(self) -> None:
        console = create_console(json_mode=False, quiet=True)
        self.assertTrue(console.quiet)


class CompactDetectionTests(TestCase):
    def test_narrow_terminal_is_compact(self) -> None:
        console, _ = make_console(width=60)
        self.assertTrue(is_compact(console))

    def test_wide_terminal_is_not_compact(self) -> None:
        console, _ = make_console(width=120)
        self.assertFalse(is_compact(console))

    def test_exactly_80_is_not_compact(self) -> None:
        console, _ = make_console(width=80)
        self.assertFalse(is_compact(console))


class BadgeTests(TestCase):
    def test_status_badge_contains_status_text(self) -> None:
        badge = status_badge("active")
        self.assertIn("active", str(badge))

    def test_agent_badge_contains_display_name(self) -> None:
        badge = agent_badge("claude")
        self.assertIn("Claude Code", str(badge))

    def test_codex_agent_badge(self) -> None:
        badge = agent_badge("codex")
        self.assertIn("Codex", str(badge))


class BannerTests(TestCase):
    def test_wide_banner_contains_brand_text(self) -> None:
        console, buf = make_console(width=120)
        render_banner(console)
        output = buf.getvalue()
        self.assertIn("Agent Relay", output)
        self.assertIn(f"v{__version__}", output)
        self.assertIn("agent-relay --help", output)

    def test_compact_banner_is_single_line(self) -> None:
        console, buf = make_console(width=60)
        render_banner(console)
        lines = [l for l in buf.getvalue().splitlines() if l.strip()]
        self.assertLessEqual(len(lines), 1)


class StartRenderTests(TestCase):
    def test_start_success_contains_session_id(self) -> None:
        console, buf = make_console()
        render_start_success(
            console,
            "20260324-120000-abc123",
            "/tmp/state.json",
            "claude",
            "Fix the bug",
        )
        output = buf.getvalue()
        self.assertIn("20260324-120000-abc123", output)
        self.assertIn("Fix the bug", output)

    def test_start_success_compact(self) -> None:
        console, buf = make_console(width=60)
        render_start_success(
            console,
            "20260324-120000-abc123",
            "/tmp/state.json",
            "claude",
            "Fix the bug",
        )
        output = buf.getvalue()
        self.assertIn("20260324-120000-abc123", output)
        self.assertIn("Session created", output)


class CheckpointRenderTests(TestCase):
    def test_checkpoint_success_contains_ids(self) -> None:
        console, buf = make_console()
        render_checkpoint_success(console, "sess-123", "cp-456")
        output = buf.getvalue()
        self.assertIn("cp-456", output)
        self.assertIn("Checkpoint saved", output)

    def test_pause_success_contains_next_action(self) -> None:
        console, buf = make_console()
        render_pause_success(console, "sess-123", "cp-456", "Hand off after review")
        output = buf.getvalue()
        self.assertIn("Session paused", output)
        self.assertIn("Hand off after review", output)

    def test_prepare_success_contains_next_action(self) -> None:
        console, buf = make_console()
        render_prepare_success(console, "sess-123", "cp-456", "Resume in Codex")
        output = buf.getvalue()
        self.assertIn("Prepared for handoff", output)
        self.assertIn("Resume in Codex", output)


class FailoverRenderTests(TestCase):
    def test_failover_shows_handoff_arrow(self) -> None:
        console, buf = make_console()
        render_failover_success(
            console,
            "claude",
            "codex",
            "rate limit",
            "/tmp/codex.md",
            "cd /tmp && codex",
        )
        output = buf.getvalue()
        self.assertIn("Handoff prepared", output)
        self.assertIn("──▶", output)


class LaunchRenderTests(TestCase):
    def test_launch_preview_shows_target(self) -> None:
        console, buf = make_console()
        render_launch_preview(
            console, "codex", "/tmp/codex.md", "cd /tmp && codex", "Start Codex"
        )
        output = buf.getvalue()
        self.assertIn("Launch preview", output)
        self.assertIn("Codex", output)

    def test_launch_result_success(self) -> None:
        console, buf = make_console()
        render_launch_result(console, True, 0)
        self.assertIn("succeeded", buf.getvalue())

    def test_launch_result_failure(self) -> None:
        console, buf = make_console()
        render_launch_result(console, False, 1)
        output = buf.getvalue()
        self.assertIn("failed", output)
        self.assertIn("1", output)


class InspectRenderTests(TestCase):
    def test_inspect_shows_session_fields(self) -> None:
        console, buf = make_console()
        session = {
            "session_id": "sess-abc",
            "current_agent": "claude",
            "current_status": "active",
            "objective": "Build the thing",
            "workstream_kind": "implementation",
            "next_action": "Write tests",
            "created_at": "2026-03-24T12:00:00Z",
            "updated_at": "2026-03-24T12:30:00Z",
            "decisions": ["Use Python"],
            "blockers": [],
            "research_notes": ["Validated the adapter boundary"],
            "implementation_notes": ["Added prepare and pause commands"],
            "touched_files": ["src/main.py"],
            "validation": {"status": "not_run", "summary": ""},
            "handoffs": [],
        }
        render_inspect(console, session)
        output = buf.getvalue()
        self.assertIn("sess-abc", output)
        self.assertIn("Build the thing", output)
        self.assertIn("Use Python", output)
        self.assertIn("Validated the adapter boundary", output)
        self.assertIn("Added prepare and pause commands", output)
        self.assertIn("src/main.py", output)


class StatusRenderTests(TestCase):
    def test_empty_status_shows_help(self) -> None:
        console, buf = make_console()
        render_dashboard(console, [])
        output = buf.getvalue()
        self.assertIn("No sessions found", output)
        self.assertIn("agent-relay codex", output)

    def test_status_shows_sessions(self) -> None:
        console, buf = make_console()
        sessions = [
            {
                "session_id": "sess-1",
                "current_agent": "claude",
                "current_status": "active",
                "objective": "First task",
                "updated_at": "2026-03-24T12:00:00Z",
            },
            {
                "session_id": "sess-2",
                "current_agent": "codex",
                "current_status": "completed",
                "objective": "Second task",
                "updated_at": "2026-03-24T11:00:00Z",
            },
        ]
        render_dashboard(console, sessions)
        output = buf.getvalue()
        self.assertIn("sess-1", output)
        self.assertIn("sess-2", output)
        self.assertIn("First task", output)


class ErrorRenderTests(TestCase):
    def test_error_contains_message(self) -> None:
        console, buf = make_console()
        render_error(console, "Something went wrong")
        self.assertIn("Something went wrong", buf.getvalue())

    def test_error_compact(self) -> None:
        console, buf = make_console(width=60)
        render_error(console, "Bad input")
        output = buf.getvalue()
        self.assertIn("Error", output)
        self.assertIn("Bad input", output)


class HelpRenderTests(TestCase):
    def test_help_includes_current_commands(self) -> None:
        console, buf = make_console()
        render_help(console)
        output = buf.getvalue()
        self.assertIn("run", output)
        self.assertIn("chat", output)
        self.assertIn("race", output)
        self.assertIn("discover", output)
        self.assertIn("status", output)
        self.assertIn("clean", output)
        self.assertIn("resolve", output)
        self.assertIn("inspect-conflicts", output)
        self.assertIn("race --continue", output)
        self.assertIn("Inspect saved", output)
        self.assertIn("artifacts", output)


class ConcurrentRenderTests(TestCase):
    def test_concurrent_result_shows_conflict_files_and_next_action(self) -> None:
        console, buf = make_console()
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact_path = Path(tmpdir) / "conflicts.json"
            artifact_path.write_text(
                json.dumps(
                    {
                        "status": "manual_resolution_required",
                        "paths": [{"path": "README.md"}, {"path": "docs/guide.md"}],
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
                elapsed_seconds=18.2,
                outcomes=(),
                conflict_artifact_path=str(artifact_path),
            )
            render_concurrent_result(console, result)
        output = buf.getvalue()
        self.assertIn("Conflicts:", output)
        self.assertIn("README.md", output)
        self.assertIn("docs/guide.md", output)
        self.assertIn("Next:", output)
        self.assertIn("agent-relay resolve", output)
        self.assertIn("sess-123", output)

    def test_concurrent_result_shows_scope_files(self) -> None:
        console, buf = make_console()
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
            elapsed_seconds=11.0,
            outcomes=(outcome,),
        )
        render_concurrent_result(console, result)
        output = buf.getvalue()
        self.assertIn("Scope:", output)
        self.assertIn("src/unexpected.py", output)


class ConflictInspectRenderTests(TestCase):
    def test_render_conflict_inspect_shows_paths_and_versions(self) -> None:
        console, buf = make_console()
        render_conflict_inspect(
            console,
            {
                "session_id": "sess-123",
                "status": "manual_resolution_required",
                "conflict_artifact_path": "/tmp/conflicts.json",
                "note": "Need a human decision.",
                "manual_paths": ["assets/logo.bin"],
                "attempted_slots": [0, 1],
                "paths": [
                    {
                        "path": "README.md",
                        "kind": "text",
                        "manual_reasons": ["lockfile"],
                        "contributors": [
                            {
                                "slot": 0,
                                "agent": "claude",
                                "roles": ["shared"],
                                "full_version_path": "/tmp/conflicts/slot-00/README.md",
                            },
                            {
                                "slot": 1,
                                "agent": "codex",
                                "roles": ["shared"],
                                "full_version_path": "/tmp/conflicts/slot-01/README.md",
                            },
                        ],
                        "base_version": {"full_path": "/tmp/conflicts/base/README.md"},
                        "repo_version": {"full_path": "/tmp/conflicts/repo/README.md"},
                    }
                ],
            },
        )
        output = buf.getvalue()
        self.assertIn("manual_resolution_required", output)
        self.assertIn("README.md", output)
        self.assertIn("lockfile", output)
        self.assertIn("Contributors:", output)
        self.assertIn("/tmp/conflicts/base/README.md", output)
        self.assertIn("/tmp/conflicts/slot-01/README.md", output)
